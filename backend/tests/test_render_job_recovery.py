"""Claims fence old workers, bound recovery and persist observable render progress."""

from contextlib import contextmanager
from datetime import timedelta
from threading import Event
from threading import enumerate as threads
from uuid import UUID

import pytest
from render_job_helpers import (
    cancel,
    expire,
    job_case,
    read,
    render_case,
    review_case,
    timeline_case,
)
from sqlalchemy import select

from arclight.application import render_leases
from arclight.application.generation_leases import LeasePolicy, clock, utc
from arclight.application.render_dispatch import candidates
from arclight.application.render_leases import claim, keep_alive, observe, owned
from arclight.core.errors import DomainError
from arclight.persistence.models import RenderAttempt, RenderExecution, RenderJob

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


def test_progress_is_persistent_monotone_and_never_claims_completion(client, job_case):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    assert run_id in candidates(factory)
    token = claim(factory, run_id)
    assert token and claim(factory, run_id) is None
    assert run_id not in candidates(factory)
    observe(factory, run_id, token, 35, phase="ENCODING")
    current = read(client, value)
    assert current["progress"] == 35 and current["phase"] == "ENCODING"
    observe(factory, run_id, token, 10)
    assert read(client, value) == current
    observe(factory, run_id, token, 100)
    assert read(client, value)["progress"] == 99
    history = client.get(f"/api/v1/render-jobs/{run_id}/attempts").json()
    assert history[0]["progress"] == 99 and history[0]["status"] == "RUNNING"
    assert cancel(client, value).status_code == 200
    with pytest.raises(DomainError, match="取消") as error:
        observe(factory, run_id, token, 100)
    assert error.value.code == "RENDER_CANCELLED"


def test_expired_claim_preserves_interruption_history_and_fences_old_worker(client, job_case):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    first = claim(factory, run_id)
    observe(factory, run_id, first, 80, phase="ENCODING")
    expire(client, value)
    assert run_id in candidates(factory)
    with pytest.raises(DomainError) as expired:
        observe(factory, run_id, first, 90)
    assert expired.value.code == "RENDER_LEASE_LOST"
    second = claim(factory, run_id)
    assert second and second != first
    with pytest.raises(DomainError) as stale:
        observe(factory, run_id, first, 99)
    assert stale.value.code == "RENDER_LEASE_LOST"
    with factory() as db:
        attempts = list(db.scalars(select(RenderAttempt).order_by(RenderAttempt.attempt_no)))
        assert [row.status for row in attempts] == ["INTERRUPTED", "RUNNING"]
        assert attempts[0].progress == 80
        assert attempts[0].error_code == "RENDER_WORKER_INTERRUPTED"
        assert attempts[0].finished_at and attempts[1].finished_at is None
        assert db.get(RenderJob, run_id).lease_token == second
    assert read(client, value)["attempt_count"] == 2


@pytest.mark.parametrize("limit", ["attempts", "elapsed"])
def test_recovery_is_bounded_and_does_not_reset_initial_deadline(client, job_case, limit):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    assert claim(factory, run_id)
    with factory() as db:
        start = utc(db.get(RenderJob, run_id).started_at)
    if limit == "attempts":
        for _ in range(2):
            expire(client, value)
            assert claim(factory, run_id)
            with factory() as db:
                assert utc(db.get(RenderJob, run_id).started_at) == start
    else:
        with factory.begin() as db:
            db.get(RenderJob, run_id).started_at = clock(db) - timedelta(seconds=1801)
    expire(client, value)
    assert claim(factory, run_id) is None
    final = read(client, value)
    assert final["status"] == "FAILED" and final["error_code"] == "RENDER_RECOVERY_EXHAUSTED"
    assert final["attempt_count"] == (3 if limit == "attempts" else 1)
    assert final["finished_at"]


def test_cancelled_expired_worker_recovers_to_cancelled_without_new_attempt(client, job_case):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    assert claim(factory, run_id)
    assert cancel(client, value).json()["status"] == "RUNNING"
    expire(client, value)
    assert claim(factory, run_id) is None
    final = read(client, value)
    assert final["status"] == "CANCELLED" and final["attempt_count"] == 1


def test_heartbeat_renews_database_deadline_and_is_joined(client, job_case, monkeypatch):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    policy = LeasePolicy(duration_seconds=4, heartbeat_seconds=0.1, poll_seconds=0.05)
    token = claim(factory, run_id, policy)
    with factory.begin() as db:
        now = clock(db)
        initial = now + timedelta(seconds=2)
        db.get(RenderJob, run_id).lease_expires_at = initial
    # Exercise renewal without making transaction/scheduler latency a lease deadline.
    monkeypatch.setattr(render_leases, "clock", lambda db: now)
    renewal_finished = Event()
    failures = []
    begin = factory.begin

    @contextmanager
    def record_renewal():
        try:
            with begin() as db:
                yield db
        except Exception as exc:
            failures.append(exc)
            raise
        finally:
            # Success is signalled only after the real transaction has committed.
            renewal_finished.set()

    monkeypatch.setattr(factory, "begin", record_renewal)
    with keep_alive(factory, run_id, token, policy):
        assert renewal_finished.wait(3), "Renderer heartbeat transaction did not finish"
        assert not failures, f"Renderer heartbeat transaction failed: {failures!r}"
        with factory() as db:
            job, render = db.get(RenderJob, run_id), db.get(RenderExecution, run_id)
            renewed = utc(job.lease_expires_at)
            assert renewed > initial
            assert renewed == now + timedelta(seconds=policy.duration_seconds)
            owned(db, render, job, token)
    assert not any(thread.name == f"render-heartbeat-{run_id}" for thread in threads())
    assert not failures, f"Renderer heartbeat transaction failed: {failures!r}"
    monkeypatch.setattr(factory, "begin", begin)
    monkeypatch.setattr(render_leases, "clock", lambda db: renewed)
    with pytest.raises(DomainError) as expired:
        observe(factory, run_id, token)
    assert expired.value.code == "RENDER_LEASE_LOST"
