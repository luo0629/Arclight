"""Real Redis and independent Celery processes. Only dedicated test DB 15 is allowed."""

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from celery import Celery
from redis import Redis
from sqlalchemy import func, select
from test_generation_jobs import capture_ok, payload
from test_generation_queues import messages, permits
from test_generation_scheduling import configure, fresh

from arclight.application.generation_leases import utc
from arclight.persistence.generation import GenerationAttempt, GenerationJob, GenerationLease
from arclight.persistence.generation_queues import GenerationPermit


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Isolated worker did not reach the expected database fact")


@pytest.fixture
def broker_workers(settings, monkeypatch, tmp_path):
    url = os.getenv("ARCLIGHT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Real Redis requires ARCLIGHT_TEST_REDIS_URL on dedicated database 15")
    parsed = urlsplit(url)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.path != "/15":
        raise ValueError("Broker tests require loopback Redis with the dedicated /15 test database")
    redis = Redis.from_url(url)
    redis.ping()
    redis.flushdb()
    celery = Celery("gen16-tests", broker=url)
    from arclight.workers import tasks

    monkeypatch.setattr(tasks.celery_app.conf, "broker_url", url)
    workers = []
    logs = []
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("ARCLIGHT_")
    }
    environment.update(
        ARCLIGHT_ENVIRONMENT="test",
        ARCLIGHT_DATABASE_URL=settings.database_url,
        ARCLIGHT_CHECKPOINT_URL=settings.checkpoint_url,
        ARCLIGHT_REDIS_URL=url,
        ARCLIGHT_PROVIDER="mock",
        ARCLIGHT_OBJECT_STORAGE_BACKEND="local",
        ARCLIGHT_ASSET_STORAGE_PATH=str(settings.asset_storage_path),
        ARCLIGHT_TEST_WORKER_CONTROL=str(tmp_path),
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=os.pathsep.join(
            [str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")]
        ),
        ARCLIGHT_MEDIA_LEASE_SECONDS="10",
        ARCLIGHT_MEDIA_HEARTBEAT_SECONDS="2",
    )

    def start(queue):
        name = "gen16-" + uuid4().hex + "@localhost"
        log = (tmp_path / (name + ".log")).open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "queue_worker_support:app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--queues=" + queue,
                "--hostname=" + name,
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=tmp_path,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        workers.append(process)
        logs.append(log)
        wait_for(lambda: celery.control.ping(destination=[name], timeout=0.3), timeout=30)
        assert process.poll() is None
        return process

    try:
        yield redis, celery, start, tmp_path
    finally:
        for process in workers:
            if process.poll() is None:
                process.terminate()
        for process in workers:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
        celery.close()
        redis.flushdb()
        redis.close()


def test_real_broker_routes_to_independent_workers_and_survives_lost_messages(
    client, episode, broker_workers
):
    redis, celery, start, _ = broker_workers
    configure(client, provider_concurrency=4, project_concurrency=4)
    batch = capture_ok(client, episode["project_id"], payload(scheduling_mode="BATCH"))
    interactive = capture_ok(client, episode["project_id"])
    from arclight.application.generation_events import publish_dispatches
    from arclight.workers.tasks import publish_generation

    factory = client.app.state.session_factory
    publish_dispatches(
        factory, publish_generation, client.app.state.settings.media_execution_policy
    )
    assert redis.llen("generation.interactive") == 1 and redis.llen("generation.batch") == 1
    # Repeated scans are cooled down; Redis messages are not authoritative job counts.
    publish_dispatches(factory, publish_generation)
    assert redis.llen("generation.interactive") == 1
    redis.delete("generation.interactive", "generation.batch")
    with factory.begin() as db:
        for row in db.scalars(select(GenerationPermit).where(GenerationPermit.state == "ISSUED")):
            row.publish_after = row.issued_at
    publish_dispatches(factory, publish_generation)
    start("generation.interactive")
    wait_for(lambda: fresh(client, interactive)["status"] == "SUCCEEDED")
    assert fresh(client, batch)["status"] == "CREATED" and redis.llen("generation.batch") == 1
    start("generation.batch")
    wait_for(lambda: fresh(client, batch)["status"] == "SUCCEEDED")
    permit = permits(client, interactive)[0]
    for _ in range(3):
        celery.send_task(
            "arclight.execute_generation",
            args=[interactive["id"], permit["dispatch_id"], permit["id"], permit["epoch"]],
            queue="generation.interactive",
        )
    wait_for(lambda: redis.llen("generation.interactive") == 0)
    assert fresh(client, interactive)["attempt_count"] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 2


def test_real_worker_death_preserves_original_attempt_and_recovery_queue(
    client, episode, broker_workers
):
    redis, celery, start, control = broker_workers
    configure(client, provider_concurrency=2, project_concurrency=2)
    body = payload(scheduling_mode="BATCH")
    body["spec"]["context"]["block_worker"] = True
    batch = capture_ok(client, episode["project_id"], body)
    interactive = capture_ok(client, episode["project_id"])
    received = messages(client)
    worker = start("generation.batch")
    batch_message = next(row for row in received if str(row[0]) == batch["id"])
    celery.send_task(
        "arclight.execute_generation",
        args=[str(value) for value in batch_message[:3]] + [batch_message[3]],
        queue="generation.batch",
    )
    wait_for(lambda: (control / batch["id"]).exists())
    # A blocked batch process cannot consume the interactive worker's execution resource.
    start("generation.interactive")
    interactive_message = next(row for row in received if str(row[0]) == interactive["id"])
    celery.send_task(
        "arclight.execute_generation",
        args=[str(value) for value in interactive_message[:3]] + [interactive_message[3]],
        queue="generation.interactive",
    )
    wait_for(lambda: fresh(client, interactive)["status"] == "SUCCEEDED")
    worker.kill()
    worker.wait(timeout=10)
    factory = client.app.state.session_factory
    from datetime import timedelta

    from arclight.application.generation_leases import clock

    with factory.begin() as db:
        lease = db.scalar(
            select(GenerationLease).where(GenerationLease.job_id == UUID(batch["id"]))
        )
        attempt_id = lease.attempt_id
        stamp = clock(db)
        lease.heartbeat_at = stamp - timedelta(seconds=2)
        lease.expires_at = stamp - timedelta(seconds=1)
    start("generation.recovery")
    celery.send_task("arclight.recover_generation", args=[batch["id"]], queue="generation.recovery")
    wait_for(
        lambda: fresh(client, batch)["status"] in {"RESULT_UNKNOWN", "MANUAL_REVIEW", "SUCCEEDED"}
    )
    with factory() as db:
        job = db.get(GenerationJob, UUID(batch["id"]))
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job.id))
        assert job.attempt_count == 1 and attempt.id == attempt_id
        assert utc(attempt.submitted_at) <= utc(job.updated_at)
        assert (
            db.scalar(
                select(func.count())
                .select_from(GenerationAttempt)
                .where(GenerationAttempt.job_id == job.id)
            )
            == 1
        )
    assert permits(client, batch)[0]["state"] == "CONSUMED"
    assert redis.llen("generation.recovery") == 0
