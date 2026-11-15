"""Human revisions use exact sources and the existing budget, execution and QA gates."""

from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_concurrency import race, require_postgres
from test_continuity_reviews import review_case
from test_generation_attempts import fail, start
from test_generation_budget import priced, set_policy
from test_generation_candidates import candidates, ingest
from test_generation_delivery import Provider, deliver
from test_generation_jobs import base
from test_shot_production import create_shot, production
from test_storyboard_table import board_fixture

from arclight.domain.shot_revisions import OPERATION_LABELS
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationAttempt,
    GenerationJob,
    Project,
    VisualAsset,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionReceipt

__all__ = ["review_case"]


def revision_path(project_id):
    return f"/api/v1/projects/{project_id}/continuity-revisions/shots"


def source_case(client, episode, board=None):
    if board is None:
        _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    shot = page["shots"][0]
    shot["request"]["spec"]["batch_size"] = 2
    response = create_shot(client, episode["project_id"], shot)
    assert response.status_code == 201, response.text
    job = response.json()
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 2
    outputs = candidates(client, episode["project_id"], job)
    return {
        "episode": episode,
        "board": board,
        "shot": shot,
        "job": job,
        "candidate": outputs[0],
        "sibling": outputs[1],
    }


@pytest.fixture
def shot_revision_case(client, episode):
    return source_case(client, episode)


def intent_for(case, operation="RECOMPOSE"):
    return {
        "candidate_id": case["candidate"]["id"],
        "storyboard_id": case["board"]["id"],
        "shot_id": case["shot"]["id"],
        "operation": operation,
        "instruction": "只调整选定范围，保留人物身份和动作接续。",
    }


def preview_command(client, case, operation="RECOMPOSE"):
    intent = intent_for(case, operation)
    response = client.post(revision_path(case["episode"]["project_id"]) + "/preview", json=intent)
    assert response.status_code == 200, response.text
    return {
        **intent,
        "confirmation_digest": response.json()["confirmation_digest"],
    }, response.json()


def submit(client, case, command, key=None):
    return client.post(
        revision_path(case["episode"]["project_id"]),
        json=command,
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def row_counts(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                GenerationJob,
                DependencyManifest,
                GenerationDispatch,
                GenerationBudgetQuote,
                AssetVersion,
            )
        )


@pytest.mark.parametrize("operation", list(OPERATION_LABELS))
def test_each_human_intent_preserves_source_and_creates_one_new_unreviewed_version(
    client, shot_revision_case, operation
):
    case = shot_revision_case
    before = row_counts(client)
    original = deepcopy(candidates(client, case["episode"]["project_id"], case["job"]))
    command, preview = preview_command(client, case, operation)
    assert row_counts(client) == before
    assert Decimal(preview["quote"]["maximum_cost"]) == 0
    assert "整图重生成" in preview["mode_description"]
    assert preview["source"]["version_id"] == case["candidate"]["asset_version_id"]
    result = submit(client, case, command)
    assert result.status_code == 201, result.text
    receipt = result.json()
    project_id = case["episode"]["project_id"]
    job = client.get(base(project_id) + "/" + receipt["job_id"]).json()
    with client.app.state.session_factory() as db:
        original_manifest = db.get(DependencyManifest, UUID(case["job"]["input_manifest_id"]))
        amended = db.get(DependencyManifest, UUID(job["input_manifest_id"]))
        old_context, new_context = (
            original_manifest.snapshot_json["context"],
            amended.snapshot_json["context"],
        )
        assert new_context["generation"]["batch_size"] == 1
        assert new_context["generation"]["output_spec"] == old_context["generation"]["output_spec"]
        assert amended.snapshot_json["inputs"] == original_manifest.snapshot_json["inputs"]
        assert new_context["continuity_review"] == old_context["continuity_review"]
        assert "continuity_revision" not in new_context
        old_blocks = {b["key"]: b for b in old_context["prompt_composition"]["blocks"]}
        new_blocks = {b["key"]: b for b in new_context["prompt_composition"]["blocks"]}
        changed = "camera_language" if operation == "RECOMPOSE" else "shot_description"
        assert all(new_blocks[k] == v for k, v in old_blocks.items() if k != changed)
        origin = new_context["prompt_composition"]["human_shot_revision"]
        assert origin["intent"]["operation"] == operation
        assert origin["source"]["version_id"] == case["candidate"]["asset_version_id"]
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    new = candidates(client, project_id, job)
    assert len(new) == 1 and new[0]["version"]["status"] == "DRAFT"
    assert new[0]["asset_version_id"] not in {row["asset_version_id"] for row in original}
    # The live asset CAS advances exactly once; candidate/version histories stay intact.
    expected_history = [row | {"asset_revision": row["asset_revision"] + 1} for row in original]
    assert candidates(client, project_id, case["job"]) == expected_history
    assert (
        new[0]["version"]["version_no"] == max(row["version"]["version_no"] for row in original) + 1
    )
    version = new[0]["version"]
    state_path = (
        f"/api/v1/projects/{project_id}/assets/{version['asset_id']}/versions/{version['id']}/state"
    )
    rejected = client.patch(
        state_path, json={"status": "READY_FOR_REVIEW", "expected_revision": version["revision"]}
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    )
    with client.app.state.session_factory() as db:
        asset = db.get(VisualAsset, UUID(version["asset_id"]))
        assert asset.current_version_id is None


def test_same_key_replays_original_command_without_another_job_or_reservation(
    client, shot_revision_case
):
    case = shot_revision_case
    command, _ = preview_command(client, case)
    key = str(uuid4())
    first = submit(client, case, command, key)
    assert first.status_code == 201, first.text
    before = row_counts(client)
    again = submit(client, case, command, key)
    assert again.status_code == 200 and again.json() == first.json()
    assert row_counts(client) == before
    conflict = submit(client, case, {**command, "instruction": "另一要求"}, key)
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert row_counts(client) == before


@pytest.mark.parametrize("change", ["version", "quote", "style", "target_lock", "board", "episode"])
def test_preview_confirmation_rechecks_sources_quote_locks_and_scope(
    client, shot_revision_case, monkeypatch, change
):
    case = shot_revision_case
    command, _ = preview_command(client, case)
    if change == "quote":
        priced(monkeypatch, "0.500001", "changed-price")
    else:
        with client.app.state.session_factory.begin() as db:
            if change == "version":
                db.get(AssetVersion, UUID(case["candidate"]["asset_version_id"])).revision += 1
            elif change == "style":
                db.get(Project, UUID(case["episode"]["project_id"])).style_guide = {
                    "description": "新规范"
                }
            elif change == "target_lock":
                db.get(
                    VisualAsset, UUID(case["candidate"]["version"]["asset_id"])
                ).status = "LOCKED"
            elif change == "board":
                db.get(AssetVersion, UUID(case["board"]["id"])).stale = True
            elif change == "episode":
                db.get(Episode, UUID(case["episode"]["id"])).status = "ARCHIVED"
    before = row_counts(client)
    response = submit(client, case, command)
    assert response.status_code == 409, response.text
    assert row_counts(client) == before


def test_unknown_quote_and_budget_exhaustion_cannot_create_revision(
    client, shot_revision_case, monkeypatch
):
    case = shot_revision_case
    project_id = case["episode"]["project_id"]
    command, _ = preview_command(client, case)
    before = row_counts(client)
    monkeypatch.setattr("arclight.providers.media_pricing.quote", lambda *args: None)
    unavailable = client.post(revision_path(project_id) + "/preview", json=intent_for(case))
    assert (
        unavailable.status_code == 422
        and unavailable.json()["error"]["code"] == "BUDGET_QUOTE_UNAVAILABLE"
    )
    assert submit(client, case, command).status_code == 422
    priced(monkeypatch, "2.000000")
    set_policy(client, project_id, limit="1.000000")
    command, _ = preview_command(client, case)
    blocked = submit(client, case, command)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"].startswith("BUDGET_")
    assert row_counts(client) == before


@pytest.mark.parametrize("mode", ["MASK", "PIXEL_EDIT"])
def test_unsupported_edit_modes_never_fall_back_silently(client, shot_revision_case, mode):
    case = shot_revision_case
    before = row_counts(client)
    response = client.post(
        revision_path(case["episode"]["project_id"]) + "/preview",
        json={**intent_for(case), "mode": mode},
    )
    assert response.status_code == 422
    assert row_counts(client) == before


@pytest.mark.parametrize("change", ["source_stale", "target_lock", "style", "board"])
def test_worker_rechecks_human_source_before_any_provider_call(client, shot_revision_case, change):
    case = shot_revision_case
    command, _ = preview_command(client, case)
    response = submit(client, case, command)
    assert response.status_code == 201, response.text
    job = client.get(base(case["episode"]["project_id"]) + "/" + response.json()["job_id"]).json()
    with client.app.state.session_factory.begin() as db:
        if change == "source_stale":
            db.get(AssetVersion, UUID(case["candidate"]["asset_version_id"])).stale = True
        elif change == "target_lock":
            db.get(VisualAsset, UUID(case["candidate"]["version"]["asset_id"])).status = "LOCKED"
        elif change == "style":
            db.get(Project, UUID(case["episode"]["project_id"])).style_guide = {
                "description": "新规范"
            }
        elif change == "board":
            db.get(AssetVersion, UUID(case["board"]["id"])).stale = True
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED"
    assert provider.requests == []


def test_unknown_execution_holds_shot_and_does_not_allow_blind_new_revision(
    client, shot_revision_case
):
    case = shot_revision_case
    command, _ = preview_command(client, case)
    key = str(uuid4())
    receipt = submit(client, case, command, key).json()
    project_id = case["episode"]["project_id"]
    job = client.get(base(project_id) + "/" + receipt["job_id"]).json()
    provider = Provider(
        receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
    )
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    before = row_counts(client)
    blocked = submit(client, case, command)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "GENERATION_SHOT_BUSY"
    assert submit(client, case, command, key).json()["job_id"] == receipt["job_id"]
    current = client.get(base(project_id) + "/" + job["id"]).json()
    assert (
        client.post(
            base(project_id) + "/" + job["id"] + "/retry",
            json={"expected_revision": current["revision"]},
        ).status_code
        == 409
    )
    assert row_counts(client) == before and len(provider.requests) == 1


def test_failed_revision_reuses_original_job_when_retried(client, shot_revision_case):
    case = shot_revision_case
    command, _ = preview_command(client, case)
    receipt = submit(client, case, command).json()
    path = base(case["episode"]["project_id"]) + "/" + receipt["job_id"]
    job = client.get(path).json()
    attempt = start(client, job)
    fail(client, job, attempt, "PROVIDER_FAILED")
    failed = client.get(path).json()
    before = row_counts(client)
    retried = client.post(path + "/retry", json={"expected_revision": failed["revision"]})
    assert retried.status_code == 200 and retried.json()["status"] == "RETRY_WAIT", retried.text
    assert retried.json()["id"] == receipt["job_id"]
    # A retry appends its delivery intent, while retaining the same job and reservation.
    assert row_counts(client) == tuple(
        count + (1 if index == 2 else 0) for index, count in enumerate(before)
    )


def test_postgres_competing_same_key_human_commands_share_one_revision(
    client, shot_revision_case, settings
):
    require_postgres(settings)
    case = shot_revision_case
    command, _ = preview_command(client, case)
    before = row_counts(client)
    responses = race(
        client.app,
        revision_path(case["episode"]["project_id"]),
        command,
        {"Authorization": "Bearer test-token", "Idempotency-Key": str(uuid4())},
    )
    assert sorted(row.status_code for row in responses) == [200, 201]
    assert responses[0].json()["job_id"] == responses[1].json()["job_id"]
    after = row_counts(client)
    assert after == tuple(value + (0 if index == 4 else 1) for index, value in enumerate(before))
