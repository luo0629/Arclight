"""GEN-05 contract: replay, delivery, provider capability, asset mutex and selection CAS."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_asset_selection import approved, choose, current
from test_asset_versions import private_objects  # noqa: F401
from test_assets import create, rename
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_generation_jobs import base, capture, capture_ok, payload
from test_generation_reconciliation import QueryFake, query

from arclight.application.generation_attempts import request_retry
from arclight.application.generation_delivery import consume_dispatch, register_receipt
from arclight.application.generation_events import publish_dispatches
from arclight.application.generation_jobs import create_job
from arclight.application.generation_leases import clock
from arclight.application.generation_state import request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationCreate, GenerationResult
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.generation_scheduling import GenerationSchedule
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
    ProjectMember,
)
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import SubmissionReceipt, SubmissionUncertain


class Provider:
    name = "mock"

    def __init__(self, *, supports=True, receipt=None, action=None):
        self.supports_idempotency = supports
        self.receipt = receipt or SubmissionReceipt(
            status="SUCCEEDED", result=GenerationResult(output_handles=["fake/image/1"])
        )
        self.action = action
        self.requests = []
        self.lock = Lock()

    def submit(self, request):
        with self.lock:
            self.requests.append(request)
        if self.action:
            self.action(request)
        return self.receipt


def event_for(client, job):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(GenerationDispatch)
            .where(GenerationDispatch.job_id == UUID(job["id"]))
            .order_by(GenerationDispatch.expected_revision.desc())
        ).id


def deliver(client, job, provider=None, event=None):
    return consume_dispatch(
        client.app.state.session_factory,
        UUID(job["id"]),
        event or event_for(client, job),
        provider or Provider(),
    )


def retry_due(client, job):
    with client.app.state.session_factory.begin() as db:
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(job["id"]))
        )
        schedule.next_attempt_at = clock(db) - timedelta(seconds=1)


def counts(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (GenerationJob, DependencyManifest, GenerationDispatch, AuditEvent)
        )


def test_idempotent_command_replays_frozen_job_without_new_records(client, episode):
    asset = create(client, episode["project_id"])
    body = payload(asset)
    first = capture(client, episode["project_id"], body, "command-17")
    assert first.status_code == 201
    before = counts(client)
    reordered = {key: body[key] for key in reversed(body)}
    reordered["spec"] = {key: body["spec"][key] for key in reversed(body["spec"])}
    assert capture(client, episode["project_id"], reordered, "command-17").json() == first.json()
    assert counts(client) == before
    assert rename(client, asset).status_code == 200
    before = counts(client)
    assert capture(client, episode["project_id"], body, "command-17").json() == first.json()
    assert counts(client) == before


@pytest.mark.parametrize(
    "field,value", [("seed", 18), ("variation", "explore-3"), ("prompt", "晴天")]
)
def test_same_key_different_fingerprint_conflicts_atomically(client, episode, field, value):
    body = payload()
    assert capture(client, episode["project_id"], body, "same-key").status_code == 201
    before = counts(client)
    body["spec"][field] = value
    response = capture(client, episode["project_id"], body, "same-key")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert counts(client) == before


def test_new_command_and_explicit_random_exploration_never_force_old_result(client, episode):
    first = capture_ok(client, episode["project_id"])
    assert deliver(client, first) == "SUCCEEDED"
    second = capture_ok(client, episode["project_id"])
    body = payload()
    body["spec"].update(seed=19, variation="new-exploration")
    third = capture_ok(client, episode["project_id"], body)
    assert len({first["id"], second["id"], third["id"]}) == 3
    with client.app.state.session_factory() as db:
        jobs = list(db.scalars(select(GenerationJob).order_by(GenerationJob.created_at)))
        assert jobs[0].request_fingerprint == jobs[1].request_fingerprint
        assert jobs[1].request_fingerprint != jobs[2].request_fingerprint
        assert jobs[1].status == jobs[2].status == "CREATED"


@pytest.mark.parametrize("supports", [True, False])
def test_duplicate_event_has_one_call_attempt_and_receipt_with_capability_record(
    client, episode, supports
):
    job = capture_ok(client, episode["project_id"])
    provider = Provider(supports=supports)
    event = event_for(client, job)
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    before = counts(client)
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    assert len(provider.requests) == 1 and counts(client) == before
    request = provider.requests[0]
    assert request.snapshot["context"]["generation"]["seed"] == 17
    assert request.idempotency_key == (f"generation:{job['id']}:1" if supports else None)
    rows = client.get(base(episode["project_id"]) + f"/{job['id']}/dispatches").json()
    assert len(rows) == 1 and rows[0]["status"] == "FINISHED"
    assert rows[0]["provider_supports_idempotency"] is supports
    assert rows[0]["provider_idempotency_key"] == request.idempotency_key
    assert rows[0]["finished_at"].endswith("Z")
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1
        attempt = db.scalar(select(GenerationAttempt))
        before = (attempt.updated_at, attempt.finished_at)
    with client.app.state.session_factory.begin() as db:
        register_receipt(db, UUID(job["id"]), request.attempt_id, provider.receipt)
    with client.app.state.session_factory() as db:
        attempt = db.get(GenerationAttempt, request.attempt_id)
        assert (attempt.updated_at, attempt.finished_at) == before


def test_receipt_conflict_and_old_attempt_replay_do_not_overwrite_retry(client, episode):
    job = capture_ok(client, episode["project_id"])
    provider = Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED"))
    assert deliver(client, job, provider) == "FAILED"
    factory = client.app.state.session_factory
    old_id = provider.requests[0].attempt_id
    with factory.begin() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        request_retry(db, row.id, row.revision, row.created_by)
    assert deliver(client, job, provider) == "RETRY_WAIT"
    retry_due(client, job)
    assert deliver(client, job, provider) == "FAILED"
    assert len(provider.requests) == 2
    assert provider.requests[0].idempotency_key != provider.requests[1].idempotency_key
    before = counts(client)
    with factory.begin() as db:
        register_receipt(db, UUID(job["id"]), old_id, provider.receipt)
    assert counts(client) == before
    with pytest.raises(DomainError, match="历史尝试"), factory.begin() as db:
        register_receipt(db, UUID(job["id"]), old_id, Provider().receipt)
    with factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).attempt_count == 2
        assert db.get(GenerationAttempt, old_id).error_code == "PROVIDER_FAILED"


@pytest.mark.parametrize(
    "failure", [TimeoutError, ConnectionError, SubmissionUncertain, RuntimeError]
)
def test_ambiguous_or_crashed_provider_never_resends_on_redelivery(client, episode, failure):
    job = capture_ok(client, episode["project_id"])

    def crash(_):
        raise failure("private adapter details")

    provider = Provider(action=crash)
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            deliver(client, job, provider)
        expected = "SUBMITTING"
    else:
        expected = "RESULT_UNKNOWN"
        assert deliver(client, job, provider) == expected
    assert deliver(client, job, provider) == expected
    assert len(provider.requests) == 1
    with client.app.state.session_factory() as db:
        event = db.get(GenerationDispatch, event_for(client, job))
        assert "private" not in str(event.receipt_json)
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1


@pytest.mark.parametrize("status", ["SUBMITTED", "RUNNING"])
def test_nonfinal_receipts_replay_and_later_final_receipt_register_once(client, episode, status):
    job = capture_ok(client, episode["project_id"])
    provider = Provider(receipt=SubmissionReceipt(status=status, request_id="provider:17"))
    assert deliver(client, job, provider) == status
    request = provider.requests[0]
    with client.app.state.session_factory.begin() as db:
        register_receipt(db, UUID(job["id"]), request.attempt_id, provider.receipt)
        register_receipt(db, UUID(job["id"]), request.attempt_id, Provider().receipt)
    assert deliver(client, job, provider) == "SUCCEEDED"
    assert len(provider.requests) == 1


def test_external_call_holds_no_business_lock_and_keeps_concurrent_cancel_intent(client, episode):
    job = capture_ok(client, episode["project_id"])

    def cancel(_):
        with client.app.state.session_factory.begin() as db:
            row = db.get(GenerationJob, UUID(job["id"]))
            request_cancel(db, row.id, row.revision, row.created_by)

    assert deliver(client, job, Provider(action=cancel)) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).cancel_requested_at is not None


@pytest.mark.parametrize("change", ["revision", "lock_version", "current_version_id"])
def test_frozen_target_cas_prevents_submission_before_external_call(client, episode, change):
    asset = create(client, episode["project_id"])
    first = approved(client, asset)
    job = capture_ok(client, episode["project_id"], payload(current(client, asset)))
    if change == "revision":
        assert rename(client, current(client, asset)).status_code == 200
    elif change == "current_version_id":
        assert choose(client, current(client, asset), first).status_code == 200
    else:
        # Exercise the independent lock-version comparison, even without a revision change.
        with client.app.state.session_factory.begin() as db:
            db.execute(text("UPDATE visual_assets SET lock_version = lock_version + 1"))
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED"
    assert provider.requests == []
    with client.app.state.session_factory() as db:
        row = db.get(GenerationDispatch, event_for(client, job))
        assert row.status == "SKIPPED" and row.receipt_json["error_code"] == "VERSION_CONFLICT"
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
    diagnostic = client.get(base(episode["project_id"]) + f"/{job['id']}/dead-letter").json()
    assert diagnostic["reason"] == "NOT_RETRYABLE" and diagnostic["category"] == "INPUT"


def test_result_registration_never_selects_asset_or_bypasses_selection_cas(client, episode):
    asset = create(client, episode["project_id"])
    version = approved(client, asset)
    asset = current(client, asset)
    job = capture_ok(client, episode["project_id"], payload(asset))
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(AssetVersion))
    assert deliver(client, job) == "SUCCEEDED"
    assert current(client, asset)["current_version_id"] is None
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AssetVersion)) == before
    assert choose(client, asset, version).status_code == 200
    assert choose(client, asset, version).status_code == 409


def test_same_asset_mutex_blocks_unknown_cancel_pending_and_failed_retry(client, episode):
    asset = create(client, episode["project_id"])
    body = payload(asset)
    first = capture_ok(client, episode["project_id"], body)
    assert capture(client, episode["project_id"], body).status_code == 409
    provider = Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED"))
    assert deliver(client, first, provider) == "FAILED"
    second = capture_ok(client, episode["project_id"], body)
    with (
        pytest.raises(DomainError, match="生成任务"),
        client.app.state.session_factory.begin() as db,
    ):
        old = db.get(GenerationJob, UUID(first["id"]))
        request_retry(db, old.id, old.revision, old.created_by)
    pending = Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="pending:17"))
    assert deliver(client, second, pending) == "SUBMITTED"
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationJob, UUID(second["id"]))
        request_cancel(db, row.id, row.revision, row.created_by)
    assert capture(client, episode["project_id"], body).status_code == 409
    with client.app.state.session_factory.begin() as db:
        register_receipt(
            db,
            UUID(second["id"]),
            pending.requests[0].attempt_id,
            SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN"),
        )
    assert capture(client, episode["project_id"], body).status_code == 409


def test_event_identity_and_stale_revision_never_start_another_attempt(client, episode):
    first = capture_ok(client, episode["project_id"])
    second = capture_ok(client, episode["project_id"])
    provider = Provider()
    with pytest.raises(DomainError):
        deliver(client, first, provider, event_for(client, second))
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationJob, UUID(first["id"]))
        request_cancel(db, row.id, row.revision, row.created_by)
    assert deliver(client, first, provider) == "CANCELLED"
    assert provider.requests == []


def test_outbox_republishes_until_claim_and_worker_duplicate_is_safe(
    client, episode, settings, monkeypatch
):
    from arclight.workers import tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    job = capture_ok(client, episode["project_id"])
    messages = []
    monkeypatch.setattr(
        tasks.execute_generation, "apply_async", lambda *, args: messages.append(args)
    )
    tasks.dispatch_outbox()
    tasks.dispatch_outbox()
    assert len(messages) == 2 and messages[0] == messages[1]
    assert messages[0] == [job["id"], str(event_for(client, job))]
    provider = Provider()
    monkeypatch.setattr(tasks, "get_media_submission_provider", lambda: provider)
    assert tasks.execute_generation(*messages[0]) == "SUCCEEDED"
    assert tasks.execute_generation(*messages[1]) == "SUCCEEDED"
    tasks.dispatch_outbox()
    assert len(messages) == 2 and len(provider.requests) == 1


def test_broker_failure_leaves_durable_ready_event(client, episode):
    job = capture_ok(client, episode["project_id"])

    def fail(*_):
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError):
        publish_dispatches(client.app.state.session_factory, fail)
    received = []
    publish_dispatches(client.app.state.session_factory, lambda *args: received.append(args))
    assert received == [(UUID(job["id"]), event_for(client, job))]


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_dispatch_history_is_project_scoped_and_bounded(client, role):
    owner = register(client, f"dispatch-owner-{role}@example.com")
    project = create_project(client)
    job = capture_ok(client, project["id"])
    user = register(client, f"dispatch-reader-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(user["user"]["id"]), role=role
                )
            )
    url = base(project["id"]) + f"/{job['id']}/dispatches"
    response = client.get(url)
    assert response.status_code == (404 if role == "outsider" else 200)
    if role != "outsider":
        assert len(response.json()) == 1
        assert client.get(url + "?limit=101").status_code == 422
        other = create_project(client)
        assert client.get(base(other["id"]) + f"/{job['id']}/dispatches").status_code == 404
    assert owner["user"]["id"] != user["user"]["id"]


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "SUCCEEDED"},
        {"status": "FAILED", "error_code": "RESULT_UNKNOWN"},
        {"status": "SUBMITTED"},
        {"status": "RESULT_UNKNOWN", "error_code": "RESULT_UNKNOWN", "cost": "1"},
        {"status": "RUNNING", "request_id": "https://private"},
    ],
)
def test_provider_receipts_are_bounded_and_cannot_invent_definite_outcomes(bad):
    with pytest.raises(ValidationError):
        SubmissionReceipt.model_validate(bad)


def test_postgres_concurrent_replay_mutex_delivery_and_receipt(client, episode, settings):
    require_postgres(settings)
    factory = client.app.state.session_factory
    asset = create(client, episode["project_id"])
    body = GenerationCreate.model_validate(payload(asset))
    barrier = Barrier(2)

    def create_once(_):
        with factory.begin() as db:
            barrier.wait(timeout=15)
            return create_job(db, UUID(episode["project_id"]), "service-owner", "race-key", body).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(create_once, range(2)))
    assert len(set(identities)) == 1
    response = capture(client, episode["project_id"], payload(asset), "other-key")
    assert response.status_code == 409
    job = {"id": str(identities[0])}
    event = event_for(client, job)
    provider = Provider()
    barrier = Barrier(2)

    def execute(_):
        barrier.wait(timeout=15)
        return deliver(client, job, provider, event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, range(2)))
    assert len(provider.requests) == 1
    barrier = Barrier(2)

    def finish(_):
        with factory.begin() as db:
            barrier.wait(timeout=15)
            register_receipt(db, identities[0], provider.requests[0].attempt_id, provider.receipt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, range(2)))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1


def test_dispatch_database_identity_claim_and_outcome_cannot_be_rewritten(client, episode):
    job = capture_ok(client, episode["project_id"])
    provider = Provider(action=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        deliver(client, job, provider)
    factory = client.app.state.session_factory
    for sql in (
        "UPDATE generation_dispatches SET expected_revision = 99",
        "UPDATE generation_dispatches SET provider_idempotency_key = 'other'",
        "UPDATE generation_dispatches SET status = 'READY', attempt_id = NULL, "
        "provider_supports_idempotency = NULL, provider_idempotency_key = NULL",
        "DELETE FROM generation_dispatches",
    ):
        with pytest.raises(DBAPIError), factory.begin() as db:
            db.execute(text(sql))
    with factory() as db:
        dialect = db.get_bind().dialect.name
    replacement = (
        "INSERT OR REPLACE INTO generation_dispatches SELECT * FROM generation_dispatches"
        if dialect == "sqlite"
        else "TRUNCATE generation_dispatches"
    )
    with pytest.raises(DBAPIError), factory.begin() as db:
        db.execute(text(replacement))


@pytest.mark.parametrize("outcome", ["FAILED", "CANCELLED", "SUCCEEDED"])
def test_definite_synchronous_outcomes_release_target_mutex(client, episode, outcome):
    asset = create(client, episode["project_id"])
    job = capture_ok(client, episode["project_id"], payload(asset))
    receipt = SubmissionReceipt(
        status=outcome,
        error_code=None
        if outcome == "SUCCEEDED"
        else outcome
        if outcome == "CANCELLED"
        else "PROVIDER_FAILED",
        result=GenerationResult(output_handles=["fake/image/17"])
        if outcome == "SUCCEEDED"
        else None,
    )
    assert deliver(client, job, Provider(receipt=receipt)) == outcome
    assert capture(client, episode["project_id"], payload(asset)).status_code == 201


def test_distinct_event_for_same_stale_revision_cannot_create_duplicate_attempt(client, episode):
    job = capture_ok(client, episode["project_id"])
    provider = Provider()
    assert deliver(client, job, provider) == "SUCCEEDED"
    with client.app.state.session_factory.begin() as db:
        original = db.get(GenerationDispatch, event_for(client, job))
        stale = GenerationDispatch(
            job_id=original.job_id,
            expected_revision=1,
            request_fingerprint=original.request_fingerprint,
        )
        db.add(stale)
        db.flush()
        stale_id = stale.id
    assert deliver(client, job, provider, stale_id) == "SUCCEEDED"
    assert len(provider.requests) == 1
    with client.app.state.session_factory() as db:
        assert db.get(GenerationDispatch, stale_id).status == "SKIPPED"
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1


def test_authoritative_not_accepted_enqueues_new_attempt_without_reusing_provider_key(
    client, episode
):
    job = capture_ok(client, episode["project_id"])
    receipt = SubmissionReceipt(
        status="RESULT_UNKNOWN", request_id="provider:17", error_code="RESULT_UNKNOWN"
    )
    old = Provider(receipt=receipt)
    assert deliver(client, job, old) == "RESULT_UNKNOWN"
    job = client.get(base(episode["project_id"]) + f"/{job['id']}").json()
    response = query(client, job, QueryFake(QueryReceipt(status="NOT_ACCEPTED")))
    assert response.status_code == 200 and response.json()["status"] == "RETRY_WAIT"
    fresh = Provider()
    assert deliver(client, job, fresh) == "RETRY_WAIT" and not fresh.requests
    retry_due(client, job)
    assert deliver(client, job, fresh) == "SUCCEEDED"
    assert len(old.requests) == len(fresh.requests) == 1
    assert old.requests[0].idempotency_key != fresh.requests[0].idempotency_key
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 2


def test_unknown_and_manual_review_hold_asset_mutex_until_definite_evidence(client, episode):
    asset = create(client, episode["project_id"])
    body = payload(asset)
    job = capture_ok(client, episode["project_id"], body)
    provider = Provider(
        receipt=SubmissionReceipt(
            status="RESULT_UNKNOWN", request_id="provider:17", error_code="RESULT_UNKNOWN"
        )
    )
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    job = client.get(base(episode["project_id"]) + f"/{job['id']}").json()
    response = query(client, job, QueryFake(QueryReceipt(status="NOT_FOUND")))
    assert response.status_code == 200 and response.json()["status"] == "MANUAL_REVIEW"
    response = capture(client, episode["project_id"], body)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "GENERATION_TARGET_BUSY"
    )


def test_invalid_adapter_receipt_becomes_unknown_without_persisting_private_content(
    client, episode
):
    job = capture_ok(client, episode["project_id"])
    invalid = SubmissionReceipt.model_construct(status="FAILED", error_code="private credentials")
    provider = Provider(receipt=invalid)
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    with client.app.state.session_factory() as db:
        assert "private" not in str(db.get(GenerationDispatch, event_for(client, job)).receipt_json)
    assert len(provider.requests) == 1


def test_delayed_original_receipt_cannot_regress_newer_result(client, episode):
    job = capture_ok(client, episode["project_id"])
    provider = Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="provider:17"))
    assert deliver(client, job, provider) == "SUBMITTED"
    factory = client.app.state.session_factory
    with factory.begin() as db:
        register_receipt(db, UUID(job["id"]), provider.requests[0].attempt_id, Provider().receipt)
    before = counts(client)
    with factory.begin() as db:
        old = register_receipt(
            db, UUID(job["id"]), provider.requests[0].attempt_id, provider.receipt
        )
        assert old.status == "SUCCEEDED"
    assert counts(client) == before
    with factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).status == "SUCCEEDED"


@pytest.mark.parametrize("case", ["different-keys-same-asset", "same-key-different-body"])
def test_postgres_command_races_return_one_conflict_without_partial_history(
    client, episode, settings, case
):
    require_postgres(settings)
    asset = create(client, episode["project_id"])
    factory = client.app.state.session_factory
    barrier = Barrier(2)

    def create_racing(index):
        body = payload(asset if case == "different-keys-same-asset" else None)
        if case == "same-key-different-body":
            body["spec"]["seed"] = index
        key = f"race-{index}" if case == "different-keys-same-asset" else "race-key"
        try:
            with factory.begin() as db:
                barrier.wait(timeout=15)
                create_job(
                    db,
                    UUID(episode["project_id"]),
                    "service-owner",
                    key,
                    GenerationCreate.model_validate(body),
                )
            return 201
        except DomainError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create_racing, range(2))) == [201, 409]
    with factory() as db:
        for model in (GenerationJob, DependencyManifest, GenerationDispatch):
            assert db.scalar(select(func.count()).select_from(model)) == 1
