"""GEN-07: admission races, bounded retries, cancellation and diagnosed replay."""

import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_collaboration import register
from test_concurrency import require_postgres
from test_generation_delivery import Provider, deliver, event_for
from test_generation_jobs import base, capture, capture_ok
from test_generation_leases import expire
from test_generation_reconciliation import QueryFake, query

from arclight.application import generation_leases as leases
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_events import publish_dispatches
from arclight.application.generation_recovery import recover_job
from arclight.application.generation_state import find_job
from arclight.core.config import Settings
from arclight.domain.generation_scheduling import ERROR_CATEGORIES, ExecutionPolicy
from arclight.persistence.generation_scheduling import (
    GenerationCall,
    GenerationDeadLetter,
    GenerationReplay,
    GenerationSchedule,
)
from arclight.persistence.models import ProjectMember
from arclight.providers.media_cancel import CancellationReceipt
from arclight.providers.media_capabilities import MOCK_CAPABILITIES
from arclight.providers.media_io import bounded_call
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import SubmissionReceipt


def configure(client, **values):
    policy = ExecutionPolicy(**values)
    client.app.state.settings.media_execution_policy = policy
    return policy


def execute(client, job, provider):
    return consume_dispatch(
        client.app.state.session_factory,
        UUID(job["id"]),
        event_for(client, job),
        provider,
        execution_policy=client.app.state.settings.media_execution_policy,
    )


def due(client, job):
    with client.app.state.session_factory.begin() as db:
        row = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(job["id"]))
        )
        row.next_attempt_at = leases.clock(db) - timedelta(seconds=1)


def fresh(client, job):
    return client.get(base(job["project_id"]) + f"/{job['id']}").json()


def schedule(client, job):
    return client.get(base(job["project_id"]) + f"/{job['id']}/schedule").json()


def dead_letter(client, job):
    response = client.get(base(job["project_id"]) + f"/{job['id']}/dead-letter")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "values",
    [
        {"provider_concurrency": 0},
        {"project_concurrency": True},
        {"max_attempts": 21},
        {"max_elapsed_seconds": float("inf")},
        {"retry_errors": ["BUDGET_EXCEEDED"]},
        {"retry_errors": [], "automatic_retry_errors": ["PROVIDER_FAILED"]},
        {"retry_base_seconds": 9, "retry_max_seconds": 2},
    ],
)
def test_policy_rejects_invalid_or_unbounded_configuration(values):
    with pytest.raises(ValidationError):
        ExecutionPolicy(**values)


def test_environment_policy_and_exponential_cap(monkeypatch):
    monkeypatch.setenv(
        "ARCLIGHT_MEDIA_EXECUTION_POLICY", '{"max_attempts":7,"retry_max_seconds":5}'
    )
    policy = Settings(_env_file=None).media_execution_policy
    assert policy.max_attempts == 7
    assert [policy.backoff(n) for n in (1, 2, 3, 20)] == [2, 4, 5, 5]


@pytest.mark.parametrize(
    "limits,blocked",
    [
        ({"provider_concurrency": 1}, "PROVIDER_CONCURRENCY"),
        ({"project_concurrency": 1}, "PROJECT_CONCURRENCY"),
        ({"provider_limits": {"mock": 1}}, "PROVIDER_CONCURRENCY"),
    ],
)
def test_async_unknown_and_manual_review_retain_generation_capacity(
    client, episode, limits, blocked
):
    configure(client, **limits)
    first = capture_ok(client, episode["project_id"])
    second = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            first,
            Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="async-17")),
        )
        == "SUBMITTED"
    )
    provider = Provider()
    assert execute(client, second, provider) == "CREATED"
    assert schedule(client, second)["blocked_code"] == blocked
    assert not provider.requests and fresh(client, second)["attempt_count"] == 0
    expire(client.app.state.session_factory, first)
    assert (
        recover_job(
            client.app.state.session_factory,
            UUID(first["id"]),
            QueryFake(QueryReceipt(status="UNKNOWN")),
        )
        == "MANUAL_REVIEW"
    )
    due(client, second)
    assert execute(client, second, provider) == "CREATED"
    current = fresh(client, first)
    assert (
        query(client, current, QueryFake(QueryReceipt(status="FAILED"))).json()["status"]
        == "FAILED"
    )
    due(client, second)
    assert execute(client, second, provider) == "SUCCEEDED" and len(provider.requests) == 1


def test_project_override_and_provider_pause_do_not_consume_attempts(client, episode):
    configure(client, project_limits={episode["project_id"]: 1, str(uuid4()): 7})
    first = capture_ok(client, episode["project_id"])
    assert schedule(client, first)["policy_json"]["project_limits"] == {episode["project_id"]: 1}
    assert (
        execute(
            client,
            first,
            Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="busy")),
        )
        == "SUBMITTED"
    )
    second = capture_ok(client, episode["project_id"])
    provider = Provider()
    assert execute(client, second, provider) == "CREATED"
    assert schedule(client, second)["blocked_code"] == "PROJECT_CONCURRENCY"
    configure(client, paused_providers={"mock"})
    third = capture_ok(client, episode["project_id"])
    assert execute(client, third, provider) == "CREATED"
    assert schedule(client, third)["blocked_code"] == "PROVIDER_PAUSED"
    assert not provider.requests


@pytest.mark.parametrize(
    "limits,blocked",
    [
        ({"global_per_minute": 1}, "GLOBAL_RATE"),
        ({"user_per_minute": 1}, "USER_RATE"),
    ],
)
def test_sliding_rate_limits_survive_sessions_and_expire(
    client, episode, monkeypatch, limits, blocked
):
    configure(client, **limits)
    first = capture_ok(client, episode["project_id"])
    assert execute(client, first, Provider()) == "SUCCEEDED"
    second = capture_ok(client, episode["project_id"])
    provider = Provider()
    assert execute(client, second, provider) == "CREATED"
    assert schedule(client, second)["blocked_code"] == blocked
    assert not provider.requests
    clock = leases.clock
    monkeypatch.setattr(leases, "clock", lambda db: clock(db) + timedelta(seconds=61))
    assert execute(client, second, provider) == "SUCCEEDED"
    assert fresh(client, second)["attempt_count"] == 1


def test_backoff_attempt_ceiling_and_manual_retry_cannot_bypass_policy(client, episode):
    configure(
        client,
        max_attempts=2,
        retry_base_seconds=20,
        retry_max_seconds=40,
        automatic_retry_errors={"PROVIDER_FAILED"},
    )
    job = capture_ok(client, episode["project_id"])
    provider = Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED"))
    assert execute(client, job, provider) == "RETRY_WAIT"
    assert schedule(client, job)["blocked_code"] == "BACKOFF"
    messages = []
    assert (
        publish_dispatches(client.app.state.session_factory, lambda *args: messages.append(args))
        == 0
    )
    assert execute(client, job, provider) == "RETRY_WAIT" and len(provider.requests) == 1
    configure(client, max_attempts=10)
    assert schedule(client, job)["policy_json"]["max_attempts"] == 2
    due(client, job)
    assert execute(client, job, provider) == "FAILED"
    diagnostic = dead_letter(client, job)
    assert diagnostic["reason"] == "ATTEMPT_LIMIT" and diagnostic["category"] == "SERVICE"
    current = fresh(client, job)
    response = client.post(
        base(job["project_id"]) + f"/{job['id']}/retry",
        json={"expected_revision": current["revision"]},
    )
    assert response.status_code == 200 and response.json()["status"] == "FAILED"
    assert execute(client, job, provider) == "FAILED" and len(provider.requests) == 2
    attempts = client.get(base(job["project_id"]) + f"/{job['id']}/attempts").json()
    assert [item["attempt_no"] for item in attempts] == [1, 2]
    assert all(item["error_category"] == "SERVICE" for item in attempts)


@pytest.mark.parametrize("error,category", list(ERROR_CATEGORIES.items()))
def test_error_classes_never_blindly_retry_nonservice_failures(client, episode, error, category):
    job = capture_ok(client, episode["project_id"])
    status = (
        "RESULT_UNKNOWN"
        if error == "RESULT_UNKNOWN"
        else ("CANCELLED" if error == "CANCELLED" else "FAILED")
    )
    result = execute(
        client, job, Provider(receipt=SubmissionReceipt(status=status, error_code=error))
    )
    expected = "RETRY_WAIT" if error == "PROVIDER_UNAVAILABLE" else status
    assert result == expected
    attempts = client.get(base(job["project_id"]) + f"/{job['id']}/attempts").json()
    assert attempts[0]["error_category"] == category
    if error in {
        "INPUT_INVALID",
        "QUALITY_FAILED",
        "STORAGE_FAILED",
        "BUDGET_EXCEEDED",
        "PROVIDER_REJECTED",
    }:
        assert dead_letter(client, job)["reason"] == "NOT_RETRYABLE"
        assert dead_letter(client, job)["category"] == category


def test_actual_timeout_is_bounded_retains_capacity_and_never_resubmits(client, episode):
    # The gateway deadline includes database admission. Leave room for it so this
    # case reaches the blocked adapter even when CI database I/O takes over 50 ms.
    configure(client, call_timeout_seconds=1, provider_concurrency=1)
    job = capture_ok(client, episode["project_id"])
    blocked, release = Event(), Event()

    def slow(_):
        blocked.set()
        assert release.wait(10)

    provider = Provider(action=slow)
    try:
        started = time.monotonic()
        assert execute(client, job, provider) == "RESULT_UNKNOWN"
        assert blocked.is_set() and time.monotonic() - started < 3
        assert execute(client, job, provider) == "RESULT_UNKNOWN" and len(provider.requests) == 1
        second = capture_ok(client, episode["project_id"])
        assert execute(client, second, Provider()) == "CREATED"
        expire(client.app.state.session_factory, job)
        assert (
            recover_job(client.app.state.session_factory, UUID(job["id"]), QueryFake())
            == "MANUAL_REVIEW"
        )
        assert dead_letter(client, job)["reason"] == "RESULT_UNKNOWN"
    finally:
        release.set()


def test_total_deadline_stops_queued_and_running_jobs_without_fabricating_failure(
    client, episode, monkeypatch
):
    configure(client, max_elapsed_seconds=5)
    queued = capture_ok(client, episode["project_id"])
    running = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            running,
            Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="running")),
        )
        == "RUNNING"
    )
    expire(client.app.state.session_factory, running)
    clock = leases.clock
    monkeypatch.setattr(leases, "clock", lambda db: clock(db) + timedelta(seconds=10))
    provider = Provider()
    assert execute(client, queued, provider) == "FAILED" and not provider.requests
    assert fresh(client, queued)["attempt_count"] == 0
    query_provider = QueryFake(QueryReceipt(status="NOT_ACCEPTED"))
    assert (
        recover_job(client.app.state.session_factory, UUID(running["id"]), query_provider)
        == "MANUAL_REVIEW"
    )
    assert not query_provider.calls
    assert (
        dead_letter(client, queued)["reason"]
        == dead_letter(client, running)["reason"]
        == "TIME_LIMIT"
    )
    with client.app.state.session_factory() as db:
        call = db.scalar(select(GenerationCall).where(GenerationCall.job_id == UUID(running["id"])))
        assert call.released_at is None


@pytest.mark.parametrize(
    "cancellation,expected",
    [
        ("CONFIRMED", "CANCELLED"),
        ("REQUESTED", "RUNNING"),
        ("UNSUPPORTED", "RUNNING"),
        ("NOT_FOUND", "RUNNING"),
        ("FAILED", "RUNNING"),
        ("RESULT_UNKNOWN", "RUNNING"),
    ],
)
def test_best_effort_cancel_needs_authoritative_confirmation(
    client, episode, cancellation, expected
):
    job = capture_ok(client, episode["project_id"])
    assert (
        deliver(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="cancel-17")),
        )
        == "SUBMITTED"
    )
    current = fresh(client, job)
    assert (
        client.post(
            base(job["project_id"]) + f"/{job['id']}/cancel",
            json={"expected_revision": current["revision"]},
        ).json()["status"]
        == "CANCEL_REQUESTED"
    )
    expire(client.app.state.session_factory, job)

    class Canceller:
        capabilities = MOCK_CAPABILITIES.model_copy(update={"supports_cancel": True})
        calls = 0

        def cancel(self, lookup):
            self.calls += 1
            with client.app.state.session_factory.begin() as db:
                row = find_job(db, UUID(job["id"]), lock=True)
                assert row.cancel_requested_at is not None
            return CancellationReceipt(status=cancellation)

    canceller = Canceller()
    query_provider = QueryFake(QueryReceipt(status="RUNNING"))
    actual = recover_job(
        client.app.state.session_factory, UUID(job["id"]), query_provider, cancel_provider=canceller
    )
    assert actual == ("CANCEL_REQUESTED" if expected == "RUNNING" else expected)
    assert canceller.calls == 1
    assert len(query_provider.calls) == (0 if cancellation == "CONFIRMED" else 1)
    with client.app.state.session_factory() as db:
        submit = db.scalar(select(GenerationCall).where(GenerationCall.kind == "SUBMIT"))
        assert (submit.released_at is not None) == (cancellation == "CONFIRMED")


def test_not_accepted_reconciliation_respects_attempt_limit(client, episode):
    configure(client, max_attempts=1)
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(
                receipt=SubmissionReceipt(
                    status="RESULT_UNKNOWN", request_id="unknown", error_code="RESULT_UNKNOWN"
                )
            ),
        )
        == "RESULT_UNKNOWN"
    )
    response = query(client, fresh(client, job), QueryFake(QueryReceipt(status="NOT_ACCEPTED")))
    assert response.json()["status"] == "FAILED"
    assert dead_letter(client, job)["reason"] == "ATTEMPT_LIMIT"
    assert fresh(client, job)["attempt_count"] == 1


@pytest.mark.parametrize(
    "error,category",
    [
        ("INPUT_INVALID", "INPUT"),
        ("QUALITY_FAILED", "QUALITY"),
        ("STORAGE_FAILED", "STORAGE"),
        ("BUDGET_EXCEEDED", "BUDGET"),
    ],
)
def test_async_failure_preserves_error_class_and_does_not_retry(client, episode, error, category):
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(
                receipt=SubmissionReceipt(
                    status="RESULT_UNKNOWN", request_id="async-error", error_code="RESULT_UNKNOWN"
                )
            ),
        )
        == "RESULT_UNKNOWN"
    )
    response = query(
        client, fresh(client, job), QueryFake(QueryReceipt(status="FAILED", error_code=error))
    )
    assert response.status_code == 200 and response.json()["status"] == "FAILED"
    assert dead_letter(client, job)["category"] == category
    attempts = client.get(base(job["project_id"]) + f"/{job['id']}/attempts").json()
    assert attempts[0]["error_code"] == error and attempts[0]["error_category"] == category


def test_owner_replay_preserves_frozen_configuration_and_lineage_and_is_idempotent(client, episode):
    configure(client, max_attempts=1)
    source = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            source,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    path = base(source["project_id"]) + f"/{source['id']}/replay"
    reserved = capture(
        client, source["project_id"], key="replay:" + hashlib.sha256(b"reviewed-replay").hexdigest()
    ).json()
    body = {
        "expected_revision": fresh(client, source)["revision"],
        "reason": "reviewed definite failure",
    }
    response = client.post(path, json=body, headers={"Idempotency-Key": "reviewed-replay"})
    assert response.status_code == 201, response.text
    replay = response.json()
    assert replay["id"] not in {source["id"], reserved["id"]} and replay["attempt_count"] == 0
    assert (
        client.post(path, json=body, headers={"Idempotency-Key": "reviewed-replay"}).json()
        == replay
    )
    assert (
        client.post(
            path, json={**body, "reason": "changed"}, headers={"Idempotency-Key": "reviewed-replay"}
        ).status_code
        == 409
    )
    history = client.get(base(source["project_id"]) + f"/{source['id']}/replays").json()
    assert len(history) == 1 and history[0]["new_job_id"] == replay["id"]
    assert history[0]["dead_letter_id"] == dead_letter(client, source)["id"]
    provider = Provider()
    assert execute(client, replay, provider) == "SUCCEEDED"
    assert provider.requests[0].snapshot["context"]["generation"]["seed"] == 17
    assert fresh(client, source)["status"] == "FAILED"
    register(client, "replay-outsider@example.com")
    assert client.post(path, json=body, headers={"Idempotency-Key": "other"}).status_code == 404


def test_unknown_cannot_replay_or_retry_and_diagnostics_are_scoped(client, episode):
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(
                receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
            ),
        )
        == "RESULT_UNKNOWN"
    )
    expire(client.app.state.session_factory, job)
    assert (
        recover_job(client.app.state.session_factory, UUID(job["id"]), QueryFake())
        == "MANUAL_REVIEW"
    )
    current = fresh(client, job)
    path = base(job["project_id"]) + f"/{job['id']}"
    assert (
        client.post(
            path + "/replay",
            json={"expected_revision": current["revision"], "reason": "review"},
            headers={"Idempotency-Key": "unsafe"},
        ).status_code
        == 409
    )
    assert (
        client.post(path + "/retry", json={"expected_revision": current["revision"]}).status_code
        == 409
    )
    assert "private" not in str(dead_letter(client, job))
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    for suffix in ("schedule", "dead-letter", "calls", "replays"):
        assert client.get(path.replace(job["project_id"], other) + "/" + suffix).status_code == 404
    for suffix in ("calls", "replays"):
        assert client.get(path + "/" + suffix, params={"limit": 1000}).status_code == 422


def test_database_guards_and_migration_preserve_diagnostics(client, episode, migrated):
    configure(client, max_attempts=1)
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    factory = client.app.state.session_factory
    for model, changes in (
        (GenerationSchedule, {"policy_json": {}}),
        (GenerationCall, {"outcome": "SUCCEEDED"}),
        (GenerationDeadLetter, {"reason": "TIME_LIMIT"}),
    ):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(update(model).values(**changes))
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0033")
    command.check(migrated)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationDeadLetter)) == 1


def test_empty_migration_roundtrip(migrated):
    command.downgrade(migrated, "0033")
    command.upgrade(migrated, "head")
    command.check(migrated)


@pytest.mark.parametrize("scope", ["provider", "project", "global", "user"])
def test_postgres_parallel_admission_is_atomic(client, episode, settings, scope):
    require_postgres(settings)
    limits = (
        {"provider_concurrency": 1}
        if scope == "provider"
        else (
            {"project_concurrency": 1}
            if scope == "project"
            else ({"global_per_minute": 1} if scope == "global" else {"user_per_minute": 1})
        )
    )
    configure(client, **limits)
    project_ids = [episode["project_id"], episode["project_id"]]
    if scope != "project":
        project_ids[1] = client.post("/api/v1/projects", json={"name": "concurrent-other"}).json()[
            "id"
        ]
    jobs = [capture_ok(client, project_id) for project_id in project_ids]
    barrier = Barrier(2)
    provider = Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="parallel"))

    def consume(job):
        barrier.wait(timeout=10)
        return execute(client, job, provider)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(consume, jobs))
    assert sorted(statuses) == ["CREATED", "RUNNING"] and len(provider.requests) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationCall)) == 1


def test_stuck_adapter_guard_has_finite_process_capacity():
    release = Event()
    try:
        for _ in range(32):
            with pytest.raises(TimeoutError):
                bounded_call(lambda: release.wait(10), 0.002)
        with pytest.raises(TimeoutError, match="capacity"):
            bounded_call(lambda: None, 0.01)
    finally:
        release.set()


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_replay_requires_owner_even_for_project_members(client, episode, role):
    configure(client, max_attempts=1)
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    current = fresh(client, job)
    member = register(client, f"replay-{role}@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(job["project_id"]), user_id=UUID(member["user"]["id"]), role=role
            )
        )
    path = base(job["project_id"]) + f"/{job['id']}"
    assert client.get(path + "/dead-letter").status_code == 200
    assert (
        client.post(
            path + "/replay",
            json={"expected_revision": current["revision"], "reason": "review"},
            headers={"Idempotency-Key": "member-replay"},
        ).status_code
        == 403
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationReplay)) == 0


def test_cancel_can_lose_to_authoritative_success(client, episode):
    job = capture_ok(client, episode["project_id"])
    assert (
        deliver(
            client, job, Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="race"))
        )
        == "RUNNING"
    )
    current = fresh(client, job)
    client.post(
        base(job["project_id"]) + f"/{job['id']}/cancel",
        json={"expected_revision": current["revision"]},
    )
    expire(client.app.state.session_factory, job)

    class Canceller:
        capabilities = MOCK_CAPABILITIES.model_copy(update={"supports_cancel": True})

        def cancel(self, _):
            return CancellationReceipt(status="REQUESTED")

    from arclight.domain.generation_requests import GenerationResult

    query_provider = QueryFake(
        QueryReceipt(status="SUCCEEDED", result=GenerationResult(output_handles=["late-success"]))
    )
    assert (
        recover_job(
            client.app.state.session_factory,
            UUID(job["id"]),
            query_provider,
            cancel_provider=Canceller(),
        )
        == "SUCCEEDED"
    )
    assert fresh(client, job)["cancel_requested_at"] is not None


def test_user_rate_is_scoped_to_creator_across_projects(client, episode):
    configure(client, user_per_minute=1)
    first = capture_ok(client, episode["project_id"])
    assert execute(client, first, Provider()) == "SUCCEEDED"
    blocked = capture_ok(client, episode["project_id"])
    assert execute(client, blocked, Provider()) == "CREATED"
    register(client, "different-media-user@example.com")
    project = client.post("/api/v1/projects", json={"name": "different-creator"}).json()
    second = capture_ok(client, project["id"])
    assert execute(client, second, Provider()) == "SUCCEEDED"


def test_scheduler_postgres_offline_sql_and_refused_offline_downgrade(migrated):
    output = io.StringIO()
    configuration = Config(str(migrated.config_file_name), output_buffer=output)
    configuration.attributes["database_url"] = "postgresql+psycopg://unused/arclight_test"
    command.upgrade(configuration, "0033:0034", sql=True)
    sql = output.getvalue()
    assert "generation_scheduler_gate" in sql and "generation_replays" in sql
    assert "BEFORE TRUNCATE" in sql and "IMMUTABLE_HISTORY" in sql
    with pytest.raises(RuntimeError, match="online preservation"):
        command.downgrade(configuration, "0034:0033", sql=True)


def test_postgres_scheduler_history_cannot_be_truncated(client, episode, settings):
    require_postgres(settings)
    configure(client, max_attempts=1)
    job = capture_ok(client, episode["project_id"])
    assert (
        execute(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    path = base(job["project_id"]) + f"/{job['id']}/replay"
    response = client.post(
        path,
        json={"expected_revision": fresh(client, job)["revision"], "reason": "review"},
        headers={"Idempotency-Key": "history-replay"},
    )
    assert response.status_code == 201
    for table in (
        "generation_schedules",
        "generation_calls",
        "generation_dead_letters",
        "generation_replays",
    ):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"TRUNCATE {table} CASCADE"))
