"""UI-04: current authority, protected references, history, rollback and QA handoff."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_asset_versions import save, state
from test_assets import create
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviews import review_case as review_case
from test_continuity_reviews import saved
from test_continuity_revision_boundaries import observe_candidate
from test_continuity_revisions import request_revision, revision_job
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import admission_fixture, approve, retarget, target_rows
from test_generation_candidates import candidates, ingest
from test_generation_delivery import Provider, deliver
from test_generation_jobs import base
from test_shot_revisions import (
    intent_for,
    preview_command,
    revision_path,
    row_counts,
    source_case,
    submit,
)
from test_shot_revisions import (
    shot_revision_case as shot_revision_case,
)
from test_storyboard_table import board_fixture
from test_visual_bible import edit, lock

from arclight.application.continuity_reviews import require_review_target
from arclight.application.continuity_revisions import request_shot_revision
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.shot_revisions import ShotRevisionCommand
from arclight.persistence.models import DependencyManifest, GenerationAttempt
from arclight.providers.media_submission import MockMediaSubmissionProvider


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_current_membership_is_required_for_preview_submission_and_same_key_replay(client, role):
    owner = register(client, "revision-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    case = source_case(client, episode)
    command, _ = preview_command(client, case)
    member = register(client, "revision-member@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    member_path = f"/api/v1/projects/{project['id']}/members"
    assert (
        client.post(member_path, json={"email": member["user"]["email"], "role": role}).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    key = str(uuid4())
    before = row_counts(client)
    preview = client.post(revision_path(project["id"]) + "/preview", json=intent_for(case))
    assert preview.status_code == (200 if role == "editor" else 403), preview.text
    accepted = submit(client, case, command, key)
    assert accepted.status_code == (201 if role == "editor" else 403), accepted.text
    if role == "viewer":
        assert row_counts(client) == before
    else:
        assert submit(client, case, command, key).status_code == 200
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.delete(member_path + "/" + member["user"]["id"]).status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    before = row_counts(client)
    assert (
        client.post(revision_path(project["id"]) + "/preview", json=intent_for(case)).status_code
        == 404
    )
    assert submit(client, case, command, key).status_code == 404
    assert row_counts(client) == before
    if role == "editor":
        client.headers["Authorization"] = "Bearer " + owner["access_token"]
        job = client.get(base(project["id"]) + "/" + accepted.json()["job_id"]).json()
        provider = Provider()
        assert deliver(client, job, provider) == "FAILED"
        assert provider.requests == []


@pytest.mark.parametrize("scope", ["project", "storyboard", "shot"])
def test_foreign_project_or_invented_precise_target_cannot_revise(
    client, shot_revision_case, scope
):
    case = shot_revision_case
    intent = intent_for(case)
    project_id = case["episode"]["project_id"]
    if scope == "project":
        project_id = client.post("/api/v1/projects", json={"name": "隔离项目"}).json()["id"]
    else:
        intent[scope + "_id"] = str(uuid4())
    before = row_counts(client)
    response = client.post(revision_path(project_id) + "/preview", json=intent)
    assert response.status_code == (404 if scope == "project" else 409), response.text
    response = client.post(
        revision_path(project_id),
        json=intent | {"confirmation_digest": "0" * 64},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == (404 if scope == "project" else 409), response.text
    assert row_counts(client) == before


@pytest.mark.parametrize(
    "change",
    [
        {"instruction": ""},
        {"instruction": " \n\t"},
        {"instruction": "修" * 2001},
        {"operation": "ERASE_IDENTITY"},
        {"provider": "client-injected"},
        {"amendments": [{"block": "character_constraints", "instruction": "overwrite"}]},
        {"candidate_id": "invalid"},
        {"mode": None},
    ],
)
def test_invalid_or_privileged_client_fields_are_rejected_without_writes(
    client, shot_revision_case, change
):
    case = shot_revision_case
    before = row_counts(client)
    path = revision_path(case["episode"]["project_id"])
    invalid = intent_for(case) | change
    assert client.post(path + "/preview", json=invalid).status_code == 422
    assert (
        client.post(
            path,
            json=invalid | {"confirmation_digest": "0" * 64},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 422
    )
    assert row_counts(client) == before


@pytest.mark.parametrize("protection", ["composition", "scene", "outfit"])
def test_locked_composition_and_exact_approved_references_are_not_overridden(
    client, episode, protection
):
    if protection == "composition":
        project = {"id": episode["project_id"]}
        assert edit(client, project, composition="人物保持在画面左侧").status_code == 201
        assert lock(client, project, 1, ["composition"]).status_code == 201
        case = source_case(client, episode)
        operation = "RECOMPOSE"
    else:
        prefix, board, body = board_fixture(client, episode)
        kind = "SCENE" if protection == "scene" else "CHARACTER"
        asset = create(client, episode["project_id"], kind=kind, name=kind)
        version = save(client, asset)
        ready = state(client, asset, version, "READY_FOR_REVIEW")
        assert ready.status_code == 200, ready.text
        assert state(client, asset, ready.json(), "APPROVED").status_code == 200
        reference = {"asset_id": asset["id"], "asset_version_id": version["id"]}
        body["content"] = deepcopy(board["content_json"])
        shot = body["content"]["shots"][0]
        if protection == "scene":
            shot["scene_asset"] = reference
        else:
            shot["character_details"] = [{"character": shot["characters"][0], "asset": reference}]
        response = client.post(prefix + "/storyboards", json=body)
        assert response.status_code == 201, response.text
        board = response.json()
        assert (
            client.post(
                prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
            ).status_code
            == 200
        )
        case = source_case(client, episode, board)
        operation = "REPLACE_SCENE" if protection == "scene" else "CHANGE_OUTFIT"
    before = row_counts(client)
    response = client.post(
        revision_path(episode["project_id"]) + "/preview", json=intent_for(case, operation)
    )
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "SHOT_REVISION_PROTECTED"
    )
    assert row_counts(client) == before
    # A compatible bounded operation still uses the protected source.
    _, preview = preview_command(client, case, "EXPRESSION")
    assert preview["source"]["version_id"] == case["candidate"]["asset_version_id"]


def test_new_revision_preserves_selected_old_image_and_requires_its_own_review(
    client, episode, review_case
):
    case = source_case(client, episode, {"id": review_case["target"]["storyboard_id"]})
    old_review = retarget(client, review_case, case["candidate"])
    record, _ = saved(client, old_review)
    admission_fixture(client, record)
    old = approve(client, old_review)
    asset, _ = target_rows(client, old_review)
    selected = choose(client, asset, old)
    assert selected.status_code == 200, selected.text
    original = candidates(client, episode["project_id"], case["job"])
    command, _ = preview_command(client, case, "EXPRESSION")
    receipt = submit(client, case, command).json()
    job = client.get(base(episode["project_id"]) + "/" + receipt["job_id"]).json()
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    new = candidates(client, episode["project_id"], job)[0]
    assert current(client, asset)["current_version_id"] == old["id"]
    assert candidates(client, episode["project_id"], case["job"]) == [
        row | {"asset_revision": row["asset_revision"] + 1} for row in original
    ]
    assert (
        state(client, asset, new["version"], "READY_FOR_REVIEW").json()["error"]["code"]
        == "CONTINUITY_REVIEW_REQUIRED"
    )
    assert choose(client, current(client, asset), new["version"]).status_code == 409
    fresh_review = retarget(client, old_review, new)
    for field in ("storyboard_id", "shot_id"):
        wrong = ReviewTarget.model_validate(fresh_review["target"] | {field: str(uuid4())})
        with client.app.state.session_factory() as db, pytest.raises(DomainError) as caught:
            require_review_target(
                db, UUID(episode["project_id"]), UUID(episode["id"]), wrong, "service-owner"
            )
        assert caught.value.code == "REVISION_TARGET_CHANGED"
    fresh, _ = saved(client, fresh_review)
    admission_fixture(client, fresh)
    new_approved = approve(client, fresh_review)
    assert current(client, asset)["current_version_id"] == old["id"]
    chosen = choose(client, current(client, asset), new_approved)
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["current_version_id"] == new["asset_version_id"]


def test_audit_failure_rolls_back_job_manifest_budget_and_dispatch(
    client, shot_revision_case, monkeypatch
):
    import arclight.application.continuity_revisions as revisions

    case = shot_revision_case
    command, _ = preview_command(client, case)
    before = row_counts(client)
    key = str(uuid4())
    real_audit = revisions.audit

    def interrupt(db, project_id, actor, action, identity, values):
        if action == "shot_revision.requested":
            raise RuntimeError("interrupted transaction")
        return real_audit(db, project_id, actor, action, identity, values)

    with monkeypatch.context() as patch:
        patch.setattr(revisions, "audit", interrupt)
        with (
            pytest.raises(RuntimeError, match="interrupted transaction"),
            client.app.state.session_factory.begin() as db,
        ):
            request_shot_revision(
                db,
                client.app.state.settings,
                UUID(case["episode"]["project_id"]),
                "service-owner",
                key,
                ShotRevisionCommand.model_validate(command),
            )
    assert row_counts(client) == before
    assert submit(client, case, command, key).status_code == 201


def test_postgres_different_keys_cannot_duplicate_same_shot_work(
    client, shot_revision_case, settings
):
    require_postgres(settings)
    case = shot_revision_case
    command, _ = preview_command(client, case)
    before = row_counts(client)

    def execute(_):
        with TestClient(client.app) as peer:
            return peer.post(
                revision_path(case["episode"]["project_id"]),
                json=command,
                headers={"Authorization": "Bearer test-token", "Idempotency-Key": str(uuid4())},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(execute, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["code"] == "GENERATION_SHOT_BUSY"
    assert row_counts(client) == tuple(
        count + (0 if index == 4 else 1) for index, count in enumerate(before)
    )


def test_qa_revision_of_human_output_uses_its_own_current_source(
    client, episode, settings, reviewer_case, model_service
):
    case = source_case(client, episode, {"id": reviewer_case["target"]["storyboard_id"]})
    command, _ = preview_command(client, case, "EXPRESSION")
    receipt = submit(client, case, command).json()
    job = client.get(base(episode["project_id"]) + "/" + receipt["job_id"]).json()
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    reviewed = observe_candidate(client, episode, settings, model_service, reviewer_case, candidate)
    result = request_revision(client, reviewed)
    assert result.status_code == 201, result.text
    qa_job = revision_job(client, reviewed, result.json())
    with client.app.state.session_factory() as db:
        context = db.get(DependencyManifest, UUID(qa_job["input_manifest_id"])).snapshot_json[
            "context"
        ]
        assert "continuity_revision" in context
        composition = context["prompt_composition"]
        assert "human_shot_revision" not in composition
        assert any(
            source["source_type"] == "HUMAN_SHOT_REVISION" for source in composition["sources"]
        )
    provider = MockMediaSubmissionProvider()
    assert deliver(client, qa_job, provider) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(qa_job["id"]))
        )
    assert ingest(client, attempt) == 1
    qa_output = candidates(client, episode["project_id"], qa_job)[0]
    assert qa_output["asset_version_id"] != candidate["asset_version_id"]
    assert qa_output["version"]["status"] == "DRAFT"


@pytest.mark.parametrize("change", ["task_type", "declared_version"])
def test_capability_changes_cannot_silently_fall_back_or_reuse_a_preview(
    client, shot_revision_case, monkeypatch, change
):
    from arclight.providers.media_capabilities import resolve_capabilities

    case = shot_revision_case
    command, _ = preview_command(client, case)
    capability = resolve_capabilities("mock", "mock-image-v1")
    updated = capability.model_copy(
        update=(
            {"task_types": frozenset({"VIDEO"})}
            if change == "task_type"
            else {"version": "changed-capability/2"}
        )
    )
    monkeypatch.setattr(
        "arclight.application.shot_revision_sources.resolve_capabilities", lambda *args: updated
    )
    before = row_counts(client)
    response = submit(client, case, command)
    assert response.status_code == (422 if change == "task_type" else 409), response.text
    assert response.json()["error"]["code"] == (
        "PROVIDER_TASK_UNSUPPORTED" if change == "task_type" else "SHOT_REVISION_PREVIEW_CHANGED"
    )
    assert row_counts(client) == before


def test_human_revision_can_retry_original_job_but_cannot_create_a_replay(
    client, shot_revision_case
):
    from arclight.providers.media_submission import SubmissionReceipt

    case = shot_revision_case
    command, _ = preview_command(client, case)
    receipt = submit(client, case, command).json()
    path = base(case["episode"]["project_id"]) + "/" + receipt["job_id"]
    job = client.get(path).json()
    provider = Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED"))
    assert deliver(client, job, provider) == "FAILED"
    failed = client.get(path).json()
    before = row_counts(client)
    replay = client.post(
        path + "/replay",
        json={
            "expected_revision": failed["revision"],
            "reason": "A revision must retry through its original job",
        },
        headers={"Idempotency-Key": "human-cannot-reset"},
    )
    assert (
        replay.status_code == 409 and replay.json()["error"]["code"] == "REVISION_REPLAY_UNSAFE"
    ), replay.text
    assert row_counts(client) == before
    retry = client.post(path + "/retry", json={"expected_revision": failed["revision"]})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "RETRY_WAIT" and retry.json()["id"] == receipt["job_id"]
