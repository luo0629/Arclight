"""QA-05 uses real persistent reviewer receipts and the existing media execution pipeline."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_asset_selection import choose
from test_asset_versions import state
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_routing import route
from test_continuity_rules import rules_storage as rules_storage
from test_generation_candidates import candidates, ingest
from test_generation_delivery import deliver

from arclight.application.continuity_reviews import require_review_target
from arclight.application.continuity_revision_store import job_revision
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_revisions import REVISION_RESOURCE
from arclight.domain.versions import digest
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    VersionBinding,
    VisualAsset,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider


@pytest.fixture
def revision_case(client, episode, settings, reviewer_case, model_service):
    _, errors, _ = install_reviewer_model(
        model_service, dynamic=False, difference=("PROP", "MEDIUM")
    )
    outcome = run_review(client, settings, reviewer_case)
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert not errors
    assert outcome.result.conclusion == "REVISION_REQUIRED"
    route(client, settings, reviewer_case)
    return reviewer_case | {
        "review": outcome.result,
        "command": {
            "review_id": str(outcome.result.result_id),
            "issue_ids": [str(outcome.result.issues[0].issue_id)],
            "action": "REVISE_PROMPT",
            "amendments": [
                {"block": "continuity_context", "instruction": "Keep the red case closed."}
            ],
        },
    }


def request_revision(client, case, *, key=None, command=None):
    return client.post(
        f"/api/v1/projects/{case['project_id']}/continuity-revisions",
        headers={"Idempotency-Key": key or str(uuid4())},
        json=command or case["command"],
    )


def revision_job(client, case, revision):
    response = client.get(
        f"/api/v1/projects/{case['project_id']}/generation-jobs/{revision['job_id']}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def execution_counts(client):
    with client.app.state.session_factory() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (
                GenerationJob,
                GenerationAttempt,
                DependencyManifest,
                OutboxEvent,
                GenerationBudgetReservation,
                GenerationBudgetSettlement,
            )
        }


def test_actual_review_creates_one_bounded_revision_and_checked_unselected_candidate(
    client, episode, settings, revision_case
):
    case = revision_case
    with client.app.state.session_factory() as db:
        old_version = db.get(AssetVersion, case["review"].target.asset_version_id)
        old_data = (old_version.checksum, deepcopy(old_version.metadata_json), old_version.status)
        original = deepcopy(
            db.get(DependencyManifest, case["review"].target.input_manifest_id).snapshot_json
        )
    before = execution_counts(client)
    key = str(uuid4())
    response = request_revision(client, case, key=key)
    assert response.status_code == 201, response.text
    revision = response.json()
    assert revision["round"] == 1 and revision["status"] == "QUEUED"
    assert revision["target"] == case["target"]
    assert revision["evidence_ids"] == sorted(
        [str(identity) for identity in case["review"].issues[0].evidence_ids]
    )
    after = execution_counts(client)
    assert after["generation_jobs"] == before["generation_jobs"] + 1
    assert after["generation_attempts"] == before["generation_attempts"]
    replay = request_revision(client, case, key=key)
    assert replay.status_code == 200 and replay.json() == revision, replay.text
    assert execution_counts(client) == after
    job = revision_job(client, case, revision)
    with client.app.state.session_factory() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        saved = job_revision(db, row)
        assert saved.revision_id == UUID(revision["revision_id"])
        manifest = db.get(DependencyManifest, row.input_manifest_id).snapshot_json
        frozen = manifest["context"]
        assert manifest["inputs"][:-1] == original["inputs"]
        assert manifest["inputs"][-1]["source_asset_version_id"] == case["target"]["storyboard_id"]
        assert manifest["inputs"][-1]["dependency_type"] == "CONTENT_DEPENDENCY"
        assert manifest["inputs"][-1]["selectors"] == ["shot:" + case["target"]["shot_id"]]
        assert frozen["generation"]["batch_size"] == 1
        assert (
            frozen["prompt_composition"]["sources"][:-1]
            == original["context"]["prompt_composition"]["sources"]
        )
        for block, old in zip(
            frozen["prompt_composition"]["blocks"],
            original["context"]["prompt_composition"]["blocks"],
            strict=True,
        ):
            if block["key"] == "continuity_context":
                assert "Keep the red case closed." in block["content"]
                assert block["content"].startswith(old["content"])
            else:
                assert block == old
        for field in ("provider", "model", "parameters", "output_spec", "seed", "variation"):
            assert frozen["generation"][field] == original["context"]["generation"][field]
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    assert ingest(client, attempt) == 0
    new_candidates = candidates(client, episode["project_id"], job)
    assert len(new_candidates) == 1
    new = new_candidates[0]
    assert new["asset_version_id"] != case["target"]["asset_version_id"]
    assert new["media_check"]["status"] == "PASSED"
    target = ReviewTarget.model_validate(
        case["target"]
        | {
            "candidate_id": new["id"],
            "asset_version_id": new["asset_version_id"],
            "input_manifest_id": new["input_manifest_id"],
            "media_check_sha256": digest(new["media_check"]),
        }
    )
    with client.app.state.session_factory() as db:
        require_review_target(
            db, UUID(episode["project_id"]), UUID(episode["id"]), target, case["actor"]
        )
        for field in ("storyboard_id", "shot_id"):
            changed = target.model_copy(update={field: uuid4()})
            with pytest.raises(DomainError) as caught:
                require_review_target(
                    db, UUID(episode["project_id"]), UUID(episode["id"]), changed, case["actor"]
                )
            assert caught.value.code == "REVISION_TARGET_CHANGED"
    root = f"/api/v1/projects/{episode['project_id']}/assets/{case['target']['asset_id']}"
    asset_read = client.get(root).json()
    version_read = client.get(root + f"/versions/{new['asset_version_id']}").json()
    ready = state(client, asset_read, version_read, "READY_FOR_REVIEW")
    assert (
        ready.status_code == 409 and ready.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    )
    selected = choose(client, asset_read, version_read)
    assert selected.status_code == 409
    with client.app.state.session_factory() as db:
        old = db.get(AssetVersion, case["review"].target.asset_version_id)
        assert (old.checksum, old.metadata_json, old.status) == old_data
        version = db.get(AssetVersion, UUID(new["asset_version_id"]))
        asset = db.get(VisualAsset, version.asset_id)
        assert version.status == "DRAFT" and asset.current_version_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == REVISION_RESOURCE)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_revision.requested")
            )
            == 1
        )
        assert digest(
            db.get(DependencyManifest, case["review"].target.input_manifest_id).snapshot_json
        ) == digest(original)


@pytest.mark.parametrize(
    "changes",
    [
        {"issue_ids": [str(uuid4())]},
        {"amendments": [{"block": "project_style", "instruction": "Change the locked style"}]},
        {"provider": "openai"},
        {"max_rounds": 100},
        {"action": "REGENERATE"},
        {"amendments": [{"block": "shot_description", "instruction": " "}]},
    ],
)
def test_unbounded_or_invented_revision_is_rejected_atomically(client, revision_case, changes):
    before = execution_counts(client)
    response = request_revision(client, revision_case, command=revision_case["command"] | changes)
    assert response.status_code == 422, response.text
    assert execution_counts(client) == before


def test_new_key_cannot_repeat_source_candidate_or_change_idempotent_scope(client, revision_case):
    response = request_revision(client, revision_case, key="fixed-revision")
    assert response.status_code == 201, response.text
    before = execution_counts(client)
    duplicate = request_revision(client, revision_case)
    assert (
        duplicate.status_code == 409
        and duplicate.json()["error"]["code"] == "REVISION_ALREADY_REQUESTED"
    )
    changed = deepcopy(revision_case["command"])
    changed["amendments"][0]["instruction"] = "Use another composition."
    conflict = request_revision(client, revision_case, key="fixed-revision", command=changed)
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert execution_counts(client) == before
