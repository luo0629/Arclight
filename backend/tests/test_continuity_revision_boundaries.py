"""Revision recovery, costs, finite limits and authority at the real service boundary."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from test_agent_model import model_service as model_service
from test_collaboration import register
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_revisions import (
    execution_counts,
    request_revision,
    revision_job,
)
from test_continuity_revisions import (
    revision_case as revision_case,
)
from test_continuity_routing import new_review_run, route
from test_continuity_rules import rules_storage as rules_storage
from test_generation_accounting import ledger, usage
from test_generation_budget import priced, set_policy
from test_generation_candidates import candidates, generate, ingest
from test_generation_delivery import Provider, deliver, event_for, retry_due
from test_generation_reconciliation import QueryFake, query

from arclight.application.continuity_revisions import request_revision as submit_revision
from arclight.application.generation_attempts import start_attempt
from arclight.core.errors import DomainError
from arclight.domain.continuity_revisions import RevisionRequest
from arclight.domain.generation_requests import GenerationResult
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    GenerationAttempt,
    Project,
    ProjectMember,
    VisualAsset,
)
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionReceipt


def checked_request(client, case):
    response = request_revision(client, case)
    assert response.status_code == 201, response.text
    return response.json()


def retry(client, job):
    return client.post(
        f"/api/v1/projects/{job['project_id']}/generation-jobs/{job['id']}/retry",
        json={"expected_revision": job["revision"]},
    )


def observe_candidate(client, episode, settings, model_service, case, candidate):
    current = new_review_run(client, episode, settings, case)
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        target = case["target"] | {
            "candidate_id": candidate["id"],
            "asset_id": str(version.asset_id),
            "asset_version_id": str(version.id),
            "input_manifest_id": candidate["input_manifest_id"],
            "media_check_sha256": digest(candidate["media_check"]),
        }
    current = current | {"target": target}
    install_reviewer_model(model_service, dynamic=False, difference=("PROP", "MEDIUM"))
    outcome = run_review(client, settings, current)
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    route(client, settings, current)
    return current | {
        "review": outcome.result,
        "command": {
            "review_id": str(outcome.result.result_id),
            "issue_ids": [str(outcome.result.issues[0].issue_id)],
            "action": "REGENERATE",
            "amendments": [],
        },
    }


def test_budget_refusal_rolls_back_and_same_key_reserves_exactly_once(
    client, episode, revision_case, monkeypatch
):
    priced(monkeypatch)
    set_policy(client, episode["project_id"], "1")
    before = execution_counts(client)
    ledger_before = ledger(client, episode["project_id"])
    rejected = request_revision(client, revision_case, key="budget-command")
    assert rejected.status_code == 409, rejected.text
    assert execution_counts(client) == before
    assert ledger(client, episode["project_id"]) == ledger_before
    set_policy(client, episode["project_id"], "5")
    response = request_revision(client, revision_case, key="budget-command")
    assert response.status_code == 201, response.text
    assert usage(client, episode["project_id"])[1] == 2
    entries = ledger(client, episode["project_id"])
    revision_entries = [row for row in entries if row["job_id"] == response.json()["job_id"]]
    assert len(revision_entries) == 1 and Decimal(revision_entries[0]["reserved"]) == 2
    replay = request_revision(client, revision_case, key="budget-command")
    assert replay.status_code == 200 and replay.json() == response.json()
    assert ledger(client, episode["project_id"]) == entries


def test_batch_problem_item_retry_preserves_successful_siblings_and_original_inputs(
    client, episode, settings, model_service, revision_case
):
    original_job, _ = generate(client, episode["project_id"], count=3)
    with client.app.state.session_factory() as db:
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(original_job["id"]))
        )
    assert ingest(client, attempt_id) == 3
    siblings = candidates(client, episode["project_id"], original_job)
    case = observe_candidate(client, episode, settings, model_service, revision_case, siblings[1])
    revision = checked_request(client, case)
    job = revision_job(client, case, revision)
    provider = Provider(
        receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED", cost=Decimal("0"))
    )
    assert deliver(client, job, provider) == "FAILED"
    failed = revision_job(client, case, revision)
    before = execution_counts(client)
    response = retry(client, failed)
    assert response.status_code == 200 and response.json()["status"] == "RETRY_WAIT", response.text
    assert execution_counts(client)["generation_jobs"] == before["generation_jobs"]
    retry_due(client, failed)
    assert deliver(client, response.json(), MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempts = list(
            db.scalars(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == UUID(job["id"]))
                .order_by(GenerationAttempt.attempt_no)
            )
        )
        assert len(attempts) == 2
        assert attempts[0].input_snapshot_json == attempts[1].input_snapshot_json
        assert attempts[0].status == "FAILED" and attempts[1].status == "SUCCEEDED"
        assert ingest(client, attempts[1].id) == 1
    after = candidates(client, episode["project_id"], original_job)
    for old, current in zip(siblings, after, strict=True):
        assert current["asset_revision"] == old["asset_revision"] + 1
        assert {key: value for key, value in current.items() if key != "asset_revision"} == {
            key: value for key, value in old.items() if key != "asset_revision"
        }
    assert len(candidates(client, episode["project_id"], job)) == 1


def test_unknown_result_keeps_reservation_and_never_resends_before_reconciliation(
    client, episode, revision_case, monkeypatch
):
    priced(monkeypatch)
    set_policy(client, episode["project_id"], "5")
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    provider = Provider(
        receipt=SubmissionReceipt(
            status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN", request_id="revision-unknown"
        )
    )
    event = event_for(client, job)
    assert deliver(client, job, provider, event) == "RESULT_UNKNOWN"
    uncertain = revision_job(client, revision_case, revision)
    before = execution_counts(client)
    assert retry(client, uncertain).status_code == 409
    assert request_revision(client, revision_case).status_code == 409
    assert deliver(client, job, provider, event) == "RESULT_UNKNOWN"
    assert len(provider.requests) == 1 and execution_counts(client) == before
    assert usage(client, episode["project_id"])[1] == 2
    with client.app.state.session_factory() as db:
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    reconciled = query(
        client,
        uncertain,
        QueryFake(
            QueryReceipt(
                status="SUCCEEDED",
                cost=Decimal("1"),
                result=GenerationResult(output_handles=[f"mock/image/{attempt_id}/0"]),
            )
        ),
    )
    assert reconciled.status_code == 200 and reconciled.json()["status"] == "SUCCEEDED", (
        reconciled.text
    )
    assert usage(client, episode["project_id"]) == (1, 0)
    with client.app.state.session_factory() as db:
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt_id) == 1
    assert len(provider.requests) == 1


def test_attempt_limit_and_dead_letter_replay_cannot_reset_revision_allowance(
    client, settings, revision_case
):
    settings.media_execution_policy = settings.media_execution_policy.model_copy(
        update={"max_attempts": 1}
    )
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    assert (
        deliver(
            client,
            job,
            Provider(
                receipt=SubmissionReceipt(
                    status="FAILED", error_code="PROVIDER_FAILED", cost=Decimal("0")
                )
            ),
        )
        == "FAILED"
    )
    failed = revision_job(client, revision_case, revision)
    before = execution_counts(client)
    response = retry(client, failed)
    assert response.status_code == 200 and response.json()["status"] == "FAILED", response.text
    replay = client.post(
        f"/api/v1/projects/{job['project_id']}/generation-jobs/{job['id']}/replay",
        headers={"Idempotency-Key": "cannot-reset"},
        json={"expected_revision": failed["revision"], "reason": "Try again outside the limit"},
    )
    assert (
        replay.status_code == 409 and replay.json()["error"]["code"] == "REVISION_REPLAY_UNSAFE"
    ), replay.text
    assert execution_counts(client) == before


def test_three_reviewed_generations_then_durable_exhaustion_without_fourth_job(
    client, episode, settings, model_service, revision_case
):
    case = revision_case
    parent_id = None
    root = case["target"]["candidate_id"]
    for round_no in range(1, 4):
        revision = checked_request(client, case)
        assert revision["round"] == round_no and revision["parent_revision_id"] == parent_id
        assert revision["root_candidate_id"] == root
        parent_id = revision["revision_id"]
        job = revision_job(client, case, revision)
        assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
        with client.app.state.session_factory() as db:
            attempt_id = db.scalar(
                select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
            )
        assert ingest(client, attempt_id) == 1
        new = candidates(client, episode["project_id"], job)[0]
        case = observe_candidate(client, episode, settings, model_service, case, new)
    before = execution_counts(client)
    response = request_revision(client, case, key="exhausted")
    assert response.status_code == 201, response.text
    stopped = response.json()
    assert stopped["status"] == "NEEDS_REVIEW" and stopped["round"] == 4
    assert stopped["job_id"] is None and stopped["input_manifest_id"] is None
    assert stopped["parent_revision_id"] == parent_id and stopped["root_candidate_id"] == root
    assert execution_counts(client) == before
    replay = request_revision(client, case, key="exhausted")
    assert replay.status_code == 200 and replay.json() == stopped
    assert request_revision(client, case).status_code == 409
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_revision.exhausted")
            )
            == 1
        )


@pytest.mark.parametrize("phase", ["REQUEST", "EXECUTE"])
@pytest.mark.parametrize("change", ["candidate_stale", "asset_locked", "project_archived"])
def test_live_input_and_lifecycle_changes_block_admission_and_worker(
    client, revision_case, phase, change
):
    revision = checked_request(client, revision_case) if phase == "EXECUTE" else None
    with client.app.state.session_factory.begin() as db:
        target = revision_case["review"].target
        if change == "candidate_stale":
            db.get(AssetVersion, target.asset_version_id).stale = True
        elif change == "asset_locked":
            db.get(VisualAsset, target.asset_id).status = "LOCKED"
        else:
            db.get(Project, UUID(revision_case["project_id"])).archived = True
    before = execution_counts(client)
    if revision is None:
        response = request_revision(client, revision_case)
        assert response.status_code == 409, response.text
    else:
        job = revision_job(client, revision_case, revision)
        with pytest.raises(DomainError), client.app.state.session_factory.begin() as db:
            start_attempt(db, UUID(job["id"]), job["revision"])
    assert execution_counts(client) == before


def test_viewer_and_cross_project_cannot_admit_or_read_revision(client, revision_case):
    revision = checked_request(client, revision_case)
    user = register(client, uuid4().hex + "@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(revision_case["project_id"]),
                user_id=UUID(user["user"]["id"]),
                role="viewer",
            )
        )
    client.headers["Authorization"] = "Bearer " + user["access_token"]
    response = request_revision(client, revision_case)
    assert response.status_code == 403, response.text
    read = client.get(
        f"/api/v1/projects/{revision_case['project_id']}/continuity-revisions/{revision['revision_id']}"
    )
    assert read.status_code == 200, read.text
    foreign = client.get(
        f"/api/v1/projects/{uuid4()}/continuity-revisions/{revision['revision_id']}"
    )
    assert foreign.status_code == 404


def test_revision_and_job_reservation_rollback_together(client, revision_case, monkeypatch):
    from arclight.application import continuity_revisions

    real_audit = continuity_revisions.audit

    def fail_audit(db, project_id, actor, action, identity, values):
        if action == "continuity_revision.requested":
            raise RuntimeError("transaction interruption")
        return real_audit(db, project_id, actor, action, identity, values)

    before = execution_counts(client)
    with monkeypatch.context() as context:
        context.setattr(continuity_revisions, "audit", fail_audit)
        with (
            pytest.raises(RuntimeError, match="transaction interruption"),
            client.app.state.session_factory.begin() as db,
        ):
            submit_revision(
                db,
                client.app.state.settings,
                UUID(revision_case["project_id"]),
                revision_case["actor"],
                "interrupted",
                RevisionRequest.model_validate(revision_case["command"]),
            )
    assert execution_counts(client) == before
    recovered = request_revision(client, revision_case, key="interrupted")
    assert recovered.status_code == 201, recovered.text


def test_postgres_duplicate_revisions_commit_one_job_and_budget_reservation(
    client, settings, revision_case
):
    require_postgres(settings)
    factory = client.app.state.session_factory

    def submit(_):
        try:
            with factory.begin() as db:
                return submit_revision(
                    db,
                    settings,
                    UUID(revision_case["project_id"]),
                    revision_case["actor"],
                    "parallel",
                    RevisionRequest.model_validate(revision_case["command"]),
                )
        except (DomainError, DBAPIError) as error:
            return error

    before = execution_counts(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert all(isinstance(outcome, tuple) for outcome in outcomes), outcomes
    assert outcomes[0][0] == outcomes[1][0]
    assert sorted(outcome[1] for outcome in outcomes) == [False, True]
    assert execution_counts(client)["generation_jobs"] == before["generation_jobs"] + 1
