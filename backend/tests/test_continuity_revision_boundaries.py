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
from test_continuity_feedback import feedback_case as feedback_case
from test_continuity_feedback import finish_revision, install_feedback_model, run_feedback
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
from test_generation_budget import priced
from test_generation_candidates import candidates, generate, ingest
from test_generation_delivery import Provider, deliver, event_for, retry_due
from test_generation_reconciliation import QueryFake, query
from test_generation_scheduling import dead_letter

from arclight.application.continuity_revision_store import revision_record
from arclight.application.continuity_revisions import request_revision as submit_revision
from arclight.application.generation_attempts import start_attempt
from arclight.core.errors import DomainError
from arclight.domain.continuity_revisions import RevisionRequest
from arclight.domain.generation_requests import GenerationResult
from arclight.domain.model_gateway import GatewayPolicy
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


def test_historical_budget_does_not_block_revision_or_duplicate_its_quote(
    client, episode, revision_case, monkeypatch
):
    priced(monkeypatch)
    project_id = episode["project_id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project_id)).budget_limit = Decimal("0.01")
    before = execution_counts(client)
    response = request_revision(client, revision_case, key="budget-command")
    assert response.status_code == 201, response.text
    after = execution_counts(client)
    assert after["generation_jobs"] == before["generation_jobs"] + 1
    assert after["generation_attempts"] == before["generation_attempts"]
    assert usage(client, project_id)[1] == 2
    entries = ledger(client, project_id)
    revision_entries = [row for row in entries if row["job_id"] == response.json()["job_id"]]
    assert len(revision_entries) == 1 and Decimal(revision_entries[0]["reserved"]) == 2
    replay = request_revision(client, revision_case, key="budget-command")
    assert replay.status_code == 200 and replay.json() == response.json()
    assert ledger(client, project_id) == entries
    assert execution_counts(client) == after


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


def test_revision_stops_automatic_retries_and_cannot_be_cloned_from_dead_letter(
    client, settings, revision_case
):
    settings.model_gateway_policy = GatewayPolicy(failure_threshold=10)
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    provider = Provider()
    for attempt_no in range(1, 4):
        provider.receipt = SubmissionReceipt(
            status="RESULT_UNKNOWN",
            request_id=f"revision-outcome-{attempt_no}",
            error_code="RESULT_UNKNOWN",
        )
        retry_due(client, job)
        assert deliver(client, job, provider) == "RESULT_UNKNOWN"
        job = revision_job(client, revision_case, revision)
        response = query(client, job, QueryFake(QueryReceipt(status="NOT_ACCEPTED")))
        assert response.status_code == 200, response.text
        job = revision_job(client, revision_case, revision)
        assert job["status"] == ("FAILED" if attempt_no == 3 else "RETRY_WAIT")
        assert job["attempt_count"] == attempt_no

    assert len(provider.requests) == 3
    assert dead_letter(client, job)["reason"] == "ATTEMPT_LIMIT"
    before = execution_counts(client)
    replay = client.post(
        f"/api/v1/projects/{job['project_id']}/generation-jobs/{job['id']}/replay",
        headers={"Idempotency-Key": "cannot-reset"},
        json={"expected_revision": job["revision"], "reason": "Try again outside the limit"},
    )
    assert (
        replay.status_code == 409 and replay.json()["error"]["code"] == "REVISION_REPLAY_UNSAFE"
    ), replay.text
    assert execution_counts(client) == before
    explicit = retry(client, job)
    assert explicit.status_code == 200 and explicit.json()["status"] == "RETRY_WAIT", explicit.text
    assert explicit.json()["id"] == job["id"] and explicit.json()["attempt_count"] == 3


def test_automatic_feedback_requests_human_review_after_three_exact_revisions(
    client, episode, settings, model_service, feedback_case
):
    case = feedback_case
    root = case["revision"]["root_candidate_id"]
    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM")
    )
    for round_no in (2, 3):
        before = execution_counts(client)
        result = run_feedback(client, settings, case)
        assert result.status == "REVISION_QUEUED", result.model_dump_json()
        after = execution_counts(client)
        assert after["generation_jobs"] == before["generation_jobs"] + 1
        assert run_feedback(client, settings, case) == result
        assert execution_counts(client) == after
        with client.app.state.session_factory() as db:
            child = revision_record(db, UUID(case["project_id"]), result.record.child_revision_id)
            assert child.parent_revision_id == UUID(case["revision"]["revision_id"])
            assert child.round == round_no and child.root_candidate_id == UUID(root)
        revision = child.model_dump(mode="json")
        candidate = finish_revision(client, episode, case, revision)
        case = case | {"revision": revision, "candidate": candidate}

    before = execution_counts(client)
    stopped = run_feedback(client, settings, case)
    assert stopped.status == "HUMAN_REVIEW", stopped.model_dump_json()
    assert stopped.record.stop_code == "REVISION_ROUND_LIMIT"
    assert stopped.record.child_revision_id is None
    assert stopped.record.human_review_request_id is not None
    assert len(sent) == 3 and not errors
    paused = execution_counts(client)
    assert paused == before | {
        "dependency_manifests": before["dependency_manifests"] + 2,
        "outbox_events": before["outbox_events"] + 1,
    }
    assert run_feedback(client, settings, case) == stopped
    assert execution_counts(client) == paused
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "continuity_feedback.decided",
                    AuditEvent.resource_id == stopped.record.feedback_id,
                )
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
