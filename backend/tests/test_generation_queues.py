"""GEN-16: durable fairness, shared capacity, fenced permits and scoped backlog."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_generation_delivery import Provider
from test_generation_jobs import base, capture, capture_ok, payload
from test_generation_scheduling import configure, execute, fresh

from arclight.application import generation_leases as leases
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_events import publish_dispatches
from arclight.application.generation_queues import issue_permits
from arclight.domain.generation_scheduling import ExecutionPolicy
from arclight.persistence.generation_queues import (
    GenerationLane,
    GenerationPermit,
    GenerationPermitEvent,
    GenerationQueueState,
)
from arclight.persistence.generation_scheduling import GenerationCall


def new(client, project_id, lane="INTERACTIVE"):
    return capture_ok(client, project_id, payload(scheduling_mode=lane))


def permits(client, job):
    return client.get(base(job["project_id"]) + f"/{job['id']}/permits").json()


def send(client, message, provider=None, policy=None):
    job, event, permit, epoch, _ = message
    return consume_dispatch(
        client.app.state.session_factory,
        job,
        event,
        provider or Provider(),
        permit_id=permit,
        permit_epoch=epoch,
        require_permit=True,
        execution_policy=policy or client.app.state.settings.media_execution_policy,
    )


def messages(client, policy=None):
    result = []
    publish_dispatches(
        client.app.state.session_factory,
        lambda *args: result.append(args),
        policy or client.app.state.settings.media_execution_policy,
    )
    return result


def advance(monkeypatch, seconds):
    original = leases.clock
    monkeypatch.setattr(leases, "clock", lambda db: original(db) + timedelta(seconds=seconds))


def test_classification_is_frozen_and_old_request_fingerprints_replay(client, episode):
    project_id = episode["project_id"]
    old = capture(client, project_id, payload(), "legacy-compatible")
    explicit = capture(
        client, project_id, payload(scheduling_mode="INTERACTIVE"), "legacy-compatible"
    )
    assert old.json() == explicit.json()
    assert (
        capture(
            client, project_id, payload(scheduling_mode="BATCH"), "legacy-compatible"
        ).status_code
        == 409
    )
    batch_body = payload(scheduling_mode="INTERACTIVE")
    batch_body["spec"]["batch_size"] = 2
    batch = capture_ok(client, project_id, batch_body)
    lane = client.get(base(project_id) + f"/{batch['id']}/queue").json()
    assert lane["lane"] == "BATCH" and lane["origin"] == "BULK_REQUEST"
    assert capture(client, project_id, payload(priority=999)).status_code == 422
    factory = client.app.state.session_factory
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        db.execute(update(GenerationLane).values(lane="INTERACTIVE"))


@pytest.mark.parametrize("resource", ["provider", "project", "global_rate", "user_rate"])
def test_batch_backlog_leaves_interactive_capacity_at_each_shared_limit(client, episode, resource):
    limits = dict(
        provider_concurrency=20, project_concurrency=20, global_per_minute=100, user_per_minute=100
    )
    limits.update(
        {
            "provider": {"provider_concurrency": 2},
            "project": {"project_concurrency": 2},
            "global_rate": {"global_per_minute": 3},
            "user_rate": {"user_per_minute": 3},
        }[resource]
    )
    policy = configure(client, **limits)
    batch = new(client, episode["project_id"], "BATCH")
    new(client, episode["project_id"], "BATCH")
    assert len(messages(client)) == 1
    interactive = new(client, episode["project_id"])
    received = messages(client)
    assert [str(message[0]) for message in received] == [interactive["id"]]
    assert send(client, received[0], policy=policy) == "SUCCEEDED"
    assert permits(client, batch)[0]["state"] == "ISSUED"


def test_weighted_successful_admissions_and_restart_safe_cursor(client, episode):
    policy = configure(
        client,
        provider_concurrency=100,
        project_concurrency=100,
        global_per_minute=1000,
        user_per_minute=1000,
        dispatch_window=1,
    )
    for lane in ["INTERACTIVE"] * 9 + ["BATCH"] * 3:
        new(client, episode["project_id"], lane)
    order = []
    for _ in range(12):
        # Every tick opens new sessions: no process-local fairness state survives.
        received = messages(client, policy)
        assert len(received) == 1
        order.append(received[0][-1])
        assert send(client, received[0], policy=policy) == "SUCCEEDED"
    assert order == ["INTERACTIVE"] * 3 + ["BATCH"] + ["INTERACTIVE"] * 3 + ["BATCH"] + [
        "INTERACTIVE"
    ] * 3 + ["BATCH"]
    with client.app.state.session_factory() as db:
        assert db.get(GenerationQueueState, "global").turn == 0


def test_projects_rotate_and_a_full_project_does_not_hide_another(client, episode):
    project_id = episode["project_id"]
    other = create_project(client)
    policy = configure(
        client,
        provider_concurrency=4,
        project_concurrency=1,
        global_per_minute=1000,
        user_per_minute=1000,
    )
    busy = new(client, project_id)
    from arclight.providers.media_submission import SubmissionReceipt

    assert (
        execute(
            client, busy, Provider(receipt=SubmissionReceipt(status="SUBMITTED", request_id="busy"))
        )
        == "SUBMITTED"
    )
    # More than the old first-100 outbox window must not head-block another project.
    from arclight.application.generation_jobs import create_job
    from arclight.domain.generation_requests import GenerationCreate

    with client.app.state.session_factory.begin() as db:
        for number in range(105):
            create_job(
                db,
                UUID(project_id),
                "service-owner",
                f"backlog-{number}",
                GenerationCreate.model_validate(payload()),
                execution_policy=policy,
            )
    ready = new(client, other["id"])
    received = messages(client)
    assert len(received) == 1 and str(received[0][0]) == ready["id"]
    assert messages(client) == []
    assert send(client, received[0]) == "SUCCEEDED"


def test_single_slot_falls_back_to_non_preemptive_weighted_order(client, episode):
    configure(client, provider_concurrency=1, project_concurrency=1)
    new(client, episode["project_id"], "BATCH")
    received = messages(client)
    assert len(received) == 1 and received[0][-1] == "BATCH"
    new(client, episode["project_id"])
    assert messages(client) == []
    assert send(client, received[0]) == "SUCCEEDED"
    assert messages(client)[0][-1] == "INTERACTIVE"


def test_expiry_and_late_epoch_cannot_duplicate_attempt_or_release_unknown_capacity(
    client, episode, monkeypatch
):
    configure(client, provider_concurrency=1, project_concurrency=1)
    job = new(client, episode["project_id"])
    old = messages(client)[0]
    advance(monkeypatch, 31)
    current = messages(client)[0]
    assert current[2] == old[2] and current[3] == old[3] + 1
    provider = Provider()
    assert send(client, old, provider) == "CREATED"
    assert not provider.requests and fresh(client, job)["attempt_count"] == 0
    from arclight.providers.media_submission import SubmissionReceipt

    unknown = Provider(
        receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
    )
    assert send(client, current, unknown) == "RESULT_UNKNOWN"
    advance(monkeypatch, 31)
    waiting = new(client, episode["project_id"])
    assert messages(client) == []
    assert permits(client, job)[0]["state"] == "CONSUMED"
    assert fresh(client, waiting)["attempt_count"] == 0
    assert send(client, current, unknown) == "RESULT_UNKNOWN" and len(unknown.requests) == 1


def test_cancellation_revokes_permit_and_duplicate_worker_does_not_spend(client, episode):
    job = new(client, episode["project_id"])
    received = messages(client)[0]
    response = client.post(
        base(job["project_id"]) + f"/{job['id']}/cancel",
        json={"expected_revision": job["revision"]},
    )
    assert response.status_code == 200
    assert permits(client, job)[0]["state"] == "REVOKED"
    provider = Provider()
    assert send(client, received, provider) == "CANCELLED" and not provider.requests


def test_live_pause_and_revoked_creator_are_rechecked_before_any_attempt(client, episode):
    job = new(client, episode["project_id"])
    message = messages(client)[0]
    configure(client, paused_providers={"mock"})
    provider = Provider()
    assert send(client, message, provider) == "CREATED" and not provider.requests
    assert fresh(client, job)["attempt_count"] == 0


def test_production_worker_refuses_legacy_message_without_permit(
    client, episode, settings, monkeypatch
):
    from test_generation_delivery import event_for

    from arclight.workers import tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    provider = Provider()
    monkeypatch.setattr(tasks, "get_media_submission_provider", lambda: provider)
    job = new(client, episode["project_id"])
    assert tasks.execute_generation(job["id"], str(event_for(client, job))) == "CREATED"
    assert fresh(client, job)["attempt_count"] == 0 and not provider.requests
    assert permits(client, job)[0]["state"] == "WAITING"


def test_projects_rotate_within_each_lane(client, episode):
    other = create_project(client)
    configure(client, provider_concurrency=10, project_concurrency=10, dispatch_window=1)
    for project in (episode["project_id"], other["id"]):
        for _ in range(3):
            new(client, project)
    projects = []
    for _ in range(6):
        message = messages(client)[0]
        with client.app.state.session_factory() as db:
            projects.append(str(db.get(GenerationPermit, message[2]).project_id))
        assert send(client, message) == "SUCCEEDED"
    assert projects[0] != projects[1] and projects[:2] * 3 == projects


def test_budget_block_does_not_spend_fair_share_or_hide_other_project(client, episode, monkeypatch):
    from test_generation_accounting import ledger, usage
    from test_generation_budget import priced, set_policy

    first = episode["project_id"]
    other = create_project(client)["id"]
    for project in (first, other):
        set_policy(client, project, "10")
    priced(monkeypatch)
    blocked = new(client, first)
    ready = new(client, other)
    set_policy(client, first, "1")
    configure(client, provider_concurrency=4, project_concurrency=4, dispatch_window=1)
    received = messages(client)
    assert len(received) == 1 and str(received[0][0]) == ready["id"]
    assert send(client, received[0]) == "SUCCEEDED"
    assert messages(client) == []
    assert permits(client, blocked)[0]["reason"] == "BUDGET_EXCEEDED"
    assert permits(client, blocked)[0]["epoch"] == 0
    assert usage(client, first) == (0, 2) and len(ledger(client, first)) == 1
    assert fresh(client, blocked)["attempt_count"] == 0


def test_paid_duplicate_expiry_and_unknown_keep_one_original_reservation(
    client, episode, monkeypatch
):
    from decimal import Decimal

    from test_generation_accounting import ledger, usage
    from test_generation_budget import priced, set_policy

    from arclight.providers.media_submission import SubmissionReceipt

    project = episode["project_id"]
    set_policy(client, project, "10")
    priced(monkeypatch)
    job = new(client, project)
    old = messages(client)[0]
    advance(monkeypatch, 31)
    current = messages(client)[0]
    assert usage(client, project) == (0, 2) and len(ledger(client, project)) == 1
    unknown = Provider(
        receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
    )
    assert send(client, old, unknown) == "CREATED" and not unknown.requests
    assert send(client, current, unknown) == "RESULT_UNKNOWN"
    advance(monkeypatch, 31)
    messages(client)
    assert send(client, current, unknown) == "RESULT_UNKNOWN"
    assert len(unknown.requests) == 1 and len(ledger(client, project)) == 1
    assert usage(client, project) == (Decimal(0), Decimal(2))
    assert permits(client, job)[0]["state"] == "CONSUMED"


def test_permission_revocation_after_publication_cannot_create_attempt(client):

    from arclight.persistence.models import ProjectMember

    owner = register(client, "queue-owner@example.com")
    project = create_project(client)
    editor = register(client, "queue-editor@example.com")
    # Fixture role setup uses the actual permission model, then the real capture API.
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(editor["user"]["id"]), role="editor"
            )
        )
    job = new(client, project["id"])
    message = messages(client)[0]
    with client.app.state.session_factory.begin() as db:
        membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == UUID(project["id"]),
                ProjectMember.user_id == UUID(editor["user"]["id"]),
            )
        )
        db.delete(membership)
    provider = Provider()
    assert send(client, message, provider) == "FAILED" and not provider.requests
    with client.app.state.session_factory() as db:
        from arclight.persistence.generation import GenerationJob

        assert db.get(GenerationJob, UUID(job["id"])).attempt_count == 0
    assert owner["user"]["id"] != editor["user"]["id"]


def test_backlog_distinguishes_waiting_execution_and_permission_scope(client, episode):
    job = new(client, episode["project_id"])
    before = client.get(base(job["project_id"]) + "/queue").json()
    assert before["scheduler_stale"] and before["lanes"][0]["ready"] == 1
    message = messages(client)[0]
    queued = client.get(base(job["project_id"]) + "/queue").json()
    assert not queued["scheduler_stale"] and queued["count_unit"] == "business_jobs"
    assert queued["broker_message_count"] is None and queued["lanes"][0]["waiting_permit"] == 1
    assert queued["lanes"][0]["oldest_waiting_at"] is not None
    assert send(client, message) == "SUCCEEDED"
    history = client.get(
        base(job["project_id"]) + f"/{job['id']}/permits/{message[2]}/events"
    ).json()
    assert [row["action"] for row in history] == ["ISSUED", "PUBLISH", "CONSUMED"]
    assert client.get(base(job["project_id"]) + "/queue").json()["lanes"][0]["terminal"] == 1
    register(client, "queue-outsider@example.com")
    assert client.get(base(job["project_id"]) + "/queue").status_code == 404
    assert client.get(base(job["project_id"]) + f"/{job['id']}/permits").status_code == 404


def test_queue_history_and_protective_downgrade(client, episode, migrated):
    job = new(client, episode["project_id"])
    messages(client)
    factory = client.app.state.session_factory
    for model in (GenerationLane, GenerationPermitEvent):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(update(model).values(created_at=func.current_timestamp()))
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            table = model.__tablename__
            sql = (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                if db.get_bind().dialect.name == "sqlite"
                else f"TRUNCATE {table} CASCADE"
            )
            db.execute(text(sql))
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0038")
    assert fresh(client, job)["attempt_count"] == 0


def test_wait_percentiles_use_nearest_rank_in_wait_duration_order(client, episode, monkeypatch):
    from datetime import datetime

    with client.app.state.session_factory() as db:
        stamp = leases.clock(db) + timedelta(seconds=100)
    monkeypatch.setattr(leases, "clock", lambda db: stamp)
    jobs = [new(client, episode["project_id"]) for _ in range(2)]
    waits = []
    for job in jobs:
        lane = client.get(base(job["project_id"]) + f"/{job['id']}/queue").json()
        waits.append((stamp - datetime.fromisoformat(lane["created_at"])).total_seconds())
    report = client.get(base(episode["project_id"]) + "/queue").json()["lanes"][0]
    assert report["wait_seconds"]["p50"] == pytest.approx(min(waits))
    assert report["wait_seconds"]["p95"] == pytest.approx(max(waits))


@pytest.mark.parametrize(
    "values",
    [
        {"interactive_weight": 0},
        {"batch_weight": True},
        {"permit_seconds": 301},
        {"permit_seconds": 5, "publish_cooldown_seconds": 5},
        {"dispatch_window": 1001},
    ],
)
def test_queue_policy_rejects_unbounded_or_unrecoverable_configuration(values):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExecutionPolicy(**values)


def test_upgrade_classifies_legacy_conservatively_without_rewriting_inputs(settings):
    from pathlib import Path
    from uuid import uuid4

    from alembic.config import Config

    from arclight.persistence.base import utcnow
    from arclight.persistence.generation import GenerationDispatch, GenerationJob
    from arclight.persistence.generation_scheduling import GenerationSchedule
    from arclight.persistence.models import DependencyManifest, Project
    from arclight.persistence.session import build_engine, session_factory

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0038")
    engine = build_engine(settings)
    factory = session_factory(engine)
    with factory.begin() as db:
        project = Project(name="legacy queue", owner_id="service-owner")
        db.add(project)
        db.flush()
        manifest = DependencyManifest(
            project_id=project.id,
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
            created_by="service-owner",
            snapshot_json={"legacy_input": "preserved"},
        )
        db.add(manifest)
        db.flush()
        job = GenerationJob(
            project_id=project.id,
            target_type="SHOT_IMAGE",
            task_type="IMAGE",
            provider="mock",
            model="mock-image-v1",
            input_manifest_id=manifest.id,
            idempotency_key="legacy",
            request_fingerprint="a" * 64,
            created_by="service-owner",
        )
        db.add(job)
        db.flush()
        db.add(
            GenerationSchedule(
                job_id=job.id,
                policy_json=ExecutionPolicy().model_dump(mode="json"),
                next_attempt_at=utcnow(),
                deadline_at=utcnow() + timedelta(seconds=900),
            )
        )
        event = GenerationDispatch(
            job_id=job.id,
            expected_revision=job.revision,
            request_fingerprint=job.request_fingerprint,
        )
        db.add(event)
        db.flush()
        job_id, manifest_id, event_id = job.id, manifest.id, event.id
    with factory() as db:
        before = db.execute(select(DependencyManifest.__table__)).mappings().one()
    command.upgrade(config, "head")
    command.check(config)
    with factory() as db:
        lane = db.scalar(select(GenerationLane).where(GenerationLane.job_id == job_id))
        permit = db.scalar(select(GenerationPermit).where(GenerationPermit.dispatch_id == event_id))
        assert lane.lane == permit.lane == "BATCH" and lane.origin == "LEGACY_UNCLASSIFIED"
        assert permit.state == "WAITING" and permit.epoch == 0
        after = db.execute(select(DependencyManifest.__table__)).mappings().one()
        assert before == after and after["id"] == manifest_id
    engine.dispose()


def test_postgres_schedulers_and_consumers_share_one_atomic_capacity(client, episode, settings):
    require_postgres(settings)
    policy = configure(client, provider_concurrency=2, project_concurrency=2)
    for _ in range(5):
        new(client, episode["project_id"])
    factory = client.app.state.session_factory
    barrier = Barrier(4)

    def schedule(_):
        barrier.wait(timeout=15)
        return issue_permits(factory, policy)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(schedule, range(4))) == 2
    received = messages(client)
    from arclight.providers.media_submission import SubmissionReceipt

    class AsyncProvider(Provider):
        def submit(self, request):
            super().submit(request)
            return SubmissionReceipt(status="SUBMITTED", request_id=f"atomic:{request.job_id}")

    provider = AsyncProvider()
    barrier = Barrier(4)

    def consume(index):
        barrier.wait(timeout=15)
        if index < 2:
            return send(client, received[index], provider)
        return issue_permits(factory, policy)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(consume, range(4)))
    assert outcomes[:2] == ["SUBMITTED", "SUBMITTED"] and outcomes[2:] == [0, 0]
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationCall)
                .where(GenerationCall.released_at.is_(None))
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationPermit)
                .where(GenerationPermit.state == "ISSUED")
            )
            == 0
        )
    assert len(provider.requests) == 2


def test_submission_rate_reserve_keeps_reconciliation_available(client, episode):
    from test_generation_reconciliation import QueryFake, query

    from arclight.providers.media_query import QueryReceipt
    from arclight.providers.media_submission import SubmissionReceipt

    configure(client, global_per_minute=3, user_per_minute=3)
    running = new(client, episode["project_id"], "BATCH")
    message = messages(client)[0]
    assert (
        send(
            client,
            message,
            Provider(
                receipt=SubmissionReceipt(
                    status="RESULT_UNKNOWN", request_id="rate-reserve", error_code="RESULT_UNKNOWN"
                )
            ),
        )
        == "RESULT_UNKNOWN"
    )
    new(client, episode["project_id"])
    assert send(client, messages(client)[0]) == "SUCCEEDED"
    waiting = new(client, episode["project_id"])
    assert messages(client) == []
    fake = QueryFake(QueryReceipt(status="RUNNING"))
    result = query(client, fresh(client, running), fake)
    assert result.status_code == 200 and len(fake.calls) == 1
    assert fresh(client, waiting)["attempt_count"] == 0


def test_paused_queue_still_expires_and_releases_only_unsubmitted_money(
    client, episode, monkeypatch
):
    from test_generation_accounting import usage
    from test_generation_budget import priced, set_policy

    set_policy(client, episode["project_id"], "4")
    priced(monkeypatch)
    job = new(client, episode["project_id"])
    configure(client, paused_providers={"mock"})
    advance(monkeypatch, 901)
    assert messages(client) == []
    assert fresh(client, job)["status"] == "FAILED"
    assert fresh(client, job)["attempt_count"] == 0
    assert usage(client, episode["project_id"]) == (0, 0)
    queue = client.get(base(episode["project_id"]) + "/queue").json()
    assert queue["lanes"][0]["dead_letter"] == 1


def test_recovery_scan_is_coalesced_across_scheduler_sessions(client, monkeypatch):
    from arclight.application.generation_queues import claim_recovery_scan

    factory = client.app.state.session_factory
    assert claim_recovery_scan(factory)
    assert not claim_recovery_scan(factory)
    advance(monkeypatch, 11)
    assert claim_recovery_scan(factory)


def test_unconsumed_window_is_bounded_across_repeated_ticks(client, episode):
    configure(client, provider_concurrency=100, project_concurrency=100, dispatch_window=2)
    for _ in range(5):
        new(client, episode["project_id"])
    first = messages(client)
    assert len(first) == 2
    for _ in range(3):
        assert messages(client) == []
    assert send(client, first[0]) == "SUCCEEDED"
    assert len(messages(client)) == 1


def test_postgres_expiry_cancel_and_old_message_preserve_money(
    client, episode, settings, monkeypatch
):
    require_postgres(settings)
    from test_generation_accounting import ledger, usage
    from test_generation_budget import priced, set_policy

    from arclight.application.generation_state import find_job, request_cancel

    set_policy(client, episode["project_id"], "4")
    priced(monkeypatch)
    cancelled = new(client, episode["project_id"])
    remaining = new(client, episode["project_id"])
    received = messages(client)
    advance(monkeypatch, 31)
    factory = client.app.state.session_factory
    barrier = Barrier(3)
    provider = Provider()

    def race(index):
        barrier.wait(timeout=15)
        if index == 0:
            with factory.begin() as db:
                job = find_job(db, UUID(cancelled["id"]), lock=True)
                request_cancel(db, job.id, job.revision, job.created_by)
            return "CANCELLED"
        if index == 1:
            return issue_permits(factory)
        return send(client, received[0], provider)

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(race, range(3)))
    assert outcomes[0] == "CANCELLED" and not provider.requests
    assert usage(client, episode["project_id"]) == (0, 2)
    assert len(ledger(client, episode["project_id"])) == 2
    assert fresh(client, remaining)["attempt_count"] == 0
    assert permits(client, cancelled)[0]["state"] == "REVOKED"
