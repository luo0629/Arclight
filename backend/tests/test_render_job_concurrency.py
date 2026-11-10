"""Independent PostgreSQL transactions serialize render claims, cancellation and publication."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import render_path
from render_job_helpers import expire, job_case, read, render_case, review_case, run, timeline_case
from sqlalchemy import func, select
from test_concurrency import race, require_postgres

from arclight.application import render_execution
from arclight.application.render_jobs import cancel_job
from arclight.application.render_leases import claim
from arclight.core.errors import DomainError
from arclight.domain.render_jobs import RenderCancel
from arclight.persistence.models import AuditEvent, RenderAttempt, RenderJob, StoredFile

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


@pytest.fixture
def postgres_job(settings, request):
    require_postgres(settings)
    return request.getfixturevalue("job_case")


def test_postgres_duplicate_submit_has_one_execution_and_audit(client, postgres_job):
    manifest, _, _ = postgres_job
    responses = race(
        client.app,
        render_path(manifest, "jobs"),
        {"creation_key": str(uuid4())},
        dict(client.headers),
    )
    assert [row.status_code for row in responses] == [202, 202], [row.text for row in responses]
    assert responses[0].json() == responses[1].json()
    run_id = UUID(responses[0].json()["id"])
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderJob.id)).where(RenderJob.id == run_id)) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "render.requested", AuditEvent.resource_id == run_id
                )
            )
            == 1
        )


@pytest.mark.parametrize("expired", [False, True])
def test_postgres_duplicate_claim_fences_exactly_one_attempt(client, postgres_job, expired):
    _, value, _ = postgres_job
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    if expired:
        assert claim(factory, run_id)
        expire(client, value)
    barrier = Barrier(2)

    def invoke(_):
        barrier.wait(timeout=20)
        return claim(factory, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sum(token is not None for token in results) == 1
    with factory() as db:
        rows = list(db.scalars(select(RenderAttempt).order_by(RenderAttempt.attempt_no)))
        assert [row.status for row in rows] == (
            ["INTERRUPTED", "RUNNING"] if expired else ["RUNNING"]
        )
        assert db.get(RenderJob, run_id).lease_token == next(token for token in results if token)


def test_postgres_cancel_claim_race_cannot_lose_cancellation(client, postgres_job):
    _, value, _ = postgres_job
    factory, run_id, barrier = client.app.state.session_factory, UUID(value["id"]), Barrier(2)

    def invoke(operation):
        barrier.wait(timeout=20)
        if operation == "claim":
            return claim(factory, run_id)
        try:
            with factory.begin() as db:
                return cancel_job(
                    db, run_id, "service-owner", RenderCancel(expected_revision=value["revision"])
                ).status
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed, cancelled = list(pool.map(invoke, ("claim", "cancel")))
    final = read(client, value)
    if claimed is None:
        assert cancelled == final["status"] == "CANCELLED"
        assert final["attempt_count"] == 0
    else:
        assert cancelled == "REVISION_CONFLICT" and final["status"] == "RUNNING"
        assert final["cancel_requested_at"] is None


def test_postgres_completion_cancel_race_has_one_terminal_result(client, postgres_job, monkeypatch):
    _, value, _ = postgres_job
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    encoded, barrier = Event(), Barrier(2)
    real_encode = render_execution.execute_media

    def encode(*args, **kwargs):
        movie = real_encode(*args, **kwargs)
        encoded.set()
        barrier.wait(timeout=20)
        return movie

    def cancel(revision):
        barrier.wait(timeout=20)
        try:
            with factory.begin() as db:
                return cancel_job(
                    db, run_id, "service-owner", RenderCancel(expected_revision=revision)
                ).status
        except DomainError as error:
            return error.code

    monkeypatch.setattr(render_execution, "execute_media", encode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rendering = pool.submit(run, client, value)
        assert encoded.wait(timeout=20)
        revision = read(client, value)["revision"]
        cancellation = pool.submit(cancel, revision)
        rendered, cancelled = rendering.result(timeout=30), cancellation.result(timeout=30)
    final = read(client, value)
    assert rendered == final["status"] and rendered in {"CANCELLED", "SUCCEEDED"}
    if cancelled == "RUNNING":
        assert final["status"] == "CANCELLED" and final["output_file_id"] is None
    else:
        assert cancelled in {"REVISION_CONFLICT", "RENDER_CANCEL_CONFLICT"}
        assert final["status"] == "SUCCEEDED" and final["output_file_id"]
    with factory() as db:
        outputs = list(
            db.scalars(select(StoredFile).where(StoredFile.original_name == "render-candidate.mp4"))
        )
        assert len(outputs) == (1 if rendered == "SUCCEEDED" else 0)
        assert db.scalar(select(func.count(RenderAttempt.id))) == 1
