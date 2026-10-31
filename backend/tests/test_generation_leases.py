"""GEN-06: fenced claims, live heartbeats, restart recovery and lock-free provider I/O."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from test_concurrency import require_postgres
from test_generation_delivery import Provider, deliver, event_for
from test_generation_jobs import base, capture_ok
from test_generation_reconciliation import QueryFake

from arclight.application import generation_leases as leases
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_recovery import recover_job, recovery_candidates
from arclight.application.generation_state import find_job, request_cancel
from arclight.core.config import Settings
from arclight.persistence.generation import (
    GenerationAttempt,
    GenerationDispatch,
    GenerationJob,
    GenerationLease,
)
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import SubmissionReceipt


def expire(factory, job):
    with factory.begin() as db:
        row = leases.lease_for(db, UUID(job["id"]))
        stamp = leases.clock(db) - timedelta(minutes=2)
        row.heartbeat_at = stamp - timedelta(seconds=10)
        row.expires_at = stamp


def crash(client, job):
    def fail(_):
        raise RuntimeError("process lost")

    with pytest.raises(RuntimeError):
        deliver(client, job, Provider(action=fail))


def test_live_claim_is_exclusive_and_external_call_can_commit_cancel(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    event = event_for(client, job)
    competing = Provider()

    def inspect_live(request):
        with factory.begin() as db:
            row = find_job(db, request.job_id, lock=True)
            lease = leases.lease_for(db, row.id)
            assert lease.epoch == 1 and lease.attempt_id == request.attempt_id
            assert leases.claim(db, row, request.attempt_id, leases.LeasePolicy()) is None
            request_cancel(db, row.id, row.revision, row.created_by)
        assert consume_dispatch(factory, UUID(job["id"]), event, competing) == "CANCEL_REQUESTED"

    assert deliver(client, job, Provider(action=inspect_live)) == "SUCCEEDED"
    assert competing.requests == []
    response = client.get(base(job["project_id"]) + f"/{job['id']}/execution")
    assert response.status_code == 200
    assert response.json()["epoch"] == 1 and response.json()["released_at"].endswith("Z")
    assert "token" not in response.json() and "owner" not in response.json()


def test_heartbeat_renews_while_provider_is_blocked(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    entered, exit_call = Event(), Event()
    policy = leases.LeasePolicy(10, 0.05, 1)

    def wait(_):
        entered.set()
        assert exit_call.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            consume_dispatch,
            factory,
            UUID(job["id"]),
            event_for(client, job),
            Provider(action=wait),
            policy=policy,
        )
        try:
            assert entered.wait(10)
            with factory() as db:
                first = db.scalar(select(GenerationLease)).heartbeat_at
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with factory() as db:
                    row = db.scalar(select(GenerationLease))
                    if row.heartbeat_at > first:
                        break
                time.sleep(0.05)
            else:
                pytest.fail("No committed heartbeat while provider was running")
            assert recover_job(factory, UUID(job["id"]), QueryFake()) == "SUBMITTING"
        finally:
            exit_call.set()
        assert future.result(timeout=15) == "SUCCEEDED"


@pytest.mark.parametrize("accepted", [False, True])
def test_restart_recovers_expired_claim_without_resubmission(client, episode, accepted):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    if accepted:
        deliver(
            client, job, Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="p:1"))
        )
    else:
        crash(client, job)
    expire(factory, job)
    assert UUID(job["id"]) in recovery_candidates(factory, leases.LeasePolicy())
    query = QueryFake(QueryReceipt(status="RUNNING"))
    result = recover_job(factory, UUID(job["id"]), query)
    assert result == ("RUNNING" if accepted else "MANUAL_REVIEW")
    assert len(query.calls) == int(accepted)
    assert deliver(client, job) == result
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1
        assert db.scalar(select(GenerationDispatch)).status == "FINISHED"
        lease = db.scalar(select(GenerationLease))
        assert lease.epoch == 2 and lease.released_at is not None
    assert recover_job(factory, UUID(job["id"]), query) == result
    assert len(query.calls) == int(accepted)


def test_polling_survives_restart_and_releases_business_locks(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    deliver(client, job, Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="p:1")))
    expire(factory, job)

    def cancel():
        with factory.begin() as db:
            row = find_job(db, UUID(job["id"]), lock=True)
            request_cancel(db, row.id, row.revision, row.created_by)

    assert (
        recover_job(factory, UUID(job["id"]), QueryFake(QueryReceipt(status="RUNNING"), cancel))
        == "RECONCILING"
    )
    expire(factory, job)
    assert (
        recover_job(factory, UUID(job["id"]), QueryFake(QueryReceipt(status="CANCELLED")))
        == "CANCELLED"
    )
    with factory() as db:
        assert db.scalar(select(GenerationAttempt)).finished_at is not None


def test_expired_worker_receipt_is_fenced_and_retained_for_diagnosis(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory

    def supersede(_):
        expire(factory, job)
        assert recover_job(factory, UUID(job["id"]), QueryFake()) == "MANUAL_REVIEW"

    assert deliver(client, job, Provider(action=supersede)) == "MANUAL_REVIEW"
    with factory() as db:
        attempt = db.scalar(select(GenerationAttempt))
        assert attempt.result_json is None and attempt.finished_at is None
        assert db.scalar(select(GenerationDispatch)).receipt_json["status"] == "RESULT_UNKNOWN"
        from arclight.persistence.models import AuditEvent

        fact = db.scalar(
            select(AuditEvent).where(AuditEvent.action == "generation.execution_superseded")
        )
        assert fact.details_json["receipt"]["status"] == "SUCCEEDED"


def test_expired_or_replaced_token_cannot_renew(client, episode):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    crash(client, job)
    with factory() as db:
        token = db.scalar(select(GenerationLease)).token
    assert not leases.heartbeat(factory, UUID(job["id"]), uuid4(), leases.LeasePolicy())
    expire(factory, job)
    assert not leases.heartbeat(factory, UUID(job["id"]), token, leases.LeasePolicy())
    recover_job(factory, UUID(job["id"]), QueryFake())
    assert not leases.heartbeat(factory, UUID(job["id"]), token, leases.LeasePolicy())


def test_crash_after_claim_before_submit_is_uncertain_and_never_resubmitted(
    client, episode, monkeypatch
):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    provider = Provider()

    @contextmanager
    def lost_process(*args):
        raise RuntimeError("crashed after commit")
        yield  # pragma: no cover

    with monkeypatch.context() as patch:
        patch.setattr(leases, "keep_alive", lost_process)
        with pytest.raises(RuntimeError):
            deliver(client, job, provider)
    assert provider.requests == []
    expire(factory, job)
    assert recover_job(factory, UUID(job["id"]), QueryFake()) == "MANUAL_REVIEW"
    assert deliver(client, job, provider) == "MANUAL_REVIEW"
    assert provider.requests == []


def test_renewal_failure_cannot_let_worker_commit_after_expiry(client, episode, monkeypatch):
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    failed = Event()

    def failed_heartbeat(*args):
        failed.set()
        raise SQLAlchemyError("private database connection details")

    def wait(_):
        assert failed.wait(10)
        expire(factory, job)

    monkeypatch.setattr(leases, "heartbeat", failed_heartbeat)
    status = consume_dispatch(
        factory,
        UUID(job["id"]),
        event_for(client, job),
        Provider(action=wait),
        policy=leases.LeasePolicy(10, 0.01, 1),
    )
    assert status == "SUBMITTING"
    assert recover_job(factory, UUID(job["id"]), QueryFake()) == "MANUAL_REVIEW"


def test_legacy_orphan_is_recovered_without_creating_a_dispatch(client, episode):
    from test_generation_attempts import start

    job = capture_ok(client, episode["project_id"])
    start(client, job)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        row.updated_at = leases.clock(db) - timedelta(minutes=2)
    assert UUID(job["id"]) in recovery_candidates(factory, leases.LeasePolicy())
    assert recover_job(factory, UUID(job["id"]), QueryFake()) == "MANUAL_REVIEW"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1


def test_lease_migration_roundtrip_and_history_preservation(client, episode, migrated):
    command.check(migrated)
    command.downgrade(migrated, "0032")
    assert "generation_leases" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    job = capture_ok(client, episode["project_id"])
    deliver(client, job)
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0032")
    command.check(migrated)
    with client.app.state.session_factory.begin() as db:
        lease = db.scalar(select(GenerationLease))
        lease.epoch = 0
        with pytest.raises(DBAPIError):
            db.flush()
        db.rollback()


def test_lease_postgres_offline_ddl_and_configuration_validation(migrated):
    output = StringIO()
    migrated.output_buffer = output
    migrated.attributes["database_url"] = "postgresql+psycopg://test:test@localhost/test"
    command.upgrade(migrated, "0032:0033", sql=True)
    assert "generation_lease_expiry" in output.getvalue()
    assert "UNIQUE (job_id)" in output.getvalue()
    with pytest.raises(RuntimeError, match="online preservation"):
        command.downgrade(migrated, "0033:0032", sql=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, media_lease_seconds=10, media_heartbeat_seconds=5)


def test_postgres_two_consumers_have_one_live_executor(client, episode, settings):
    require_postgres(settings)
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    event = event_for(client, job)
    barrier = Barrier(2)
    provider = Provider()

    def run(_):
        barrier.wait(timeout=15)
        return consume_dispatch(factory, UUID(job["id"]), event, provider)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert len(provider.requests) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationLease)) == 1


def test_postgres_recovery_query_has_single_owner_and_no_business_locks(client, episode, settings):
    require_postgres(settings)
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory
    deliver(client, job, Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="p:1")))
    expire(factory, job)
    entered, finish = Event(), Event()

    def blocked_query():
        entered.set()
        assert finish.wait(15)

    query = QueryFake(QueryReceipt(status="CANCELLED"), blocked_query)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(recover_job, factory, UUID(job["id"]), query)
        try:
            assert entered.wait(15)
            assert recover_job(factory, UUID(job["id"]), query) == "RECONCILING"
            with factory.begin() as db:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                row = find_job(db, UUID(job["id"]), lock=True)
                request_cancel(db, row.id, row.revision, row.created_by)
        finally:
            finish.set()
        assert pending.result(timeout=15) == "RECONCILING"
    assert len(query.calls) == 1
    expire(factory, job)
    assert (
        recover_job(factory, UUID(job["id"]), QueryFake(QueryReceipt(status="CANCELLED")))
        == "CANCELLED"
    )


def test_beat_publishes_expired_recovery_and_worker_uses_it(client, episode, settings, monkeypatch):
    from arclight.workers import tasks

    job = capture_ok(client, episode["project_id"])
    crash(client, job)
    expire(client.app.state.session_factory, job)
    published = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tasks.recover_generation, "apply_async", lambda *, args: published.append(args)
    )
    tasks.dispatch_outbox()
    assert published == [[job["id"]]]
    assert tasks.recover_generation(job["id"]) == "MANUAL_REVIEW"
