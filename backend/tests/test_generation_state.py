"""GEN-02: full transition matrix, durable cancellation and real database races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import func, select
from test_collaboration import register
from test_concurrency import require_postgres
from test_generation_schema import add_job
from test_generation_schema import generation_seed as generation_seed

from arclight.application.commands import require_project
from arclight.application.generation_state import find_job, request_cancel, transition
from arclight.core.errors import DomainError
from arclight.domain.generation import STATUS_LABELS, GenerationStatus, validate_transition
from arclight.persistence.models import AuditEvent, GenerationJob


@pytest.mark.parametrize(
    "path",
    [
        ("CREATED", "SUBMITTING", "SUBMITTED", "RUNNING", "SUCCEEDED"),
        ("CREATED", "CANCELLED"),
        ("CREATED", "SUBMITTING", "SUCCEEDED"),
        ("CREATED", "SUBMITTING", "FAILED", "RETRY_WAIT", "SUBMITTING", "SUBMITTED"),
        ("CREATED", "SUBMITTING", "RESULT_UNKNOWN", "RECONCILING", "RUNNING", "SUCCEEDED"),
        ("CREATED", "SUBMITTING", "RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW", "RECONCILING"),
        ("CREATED", "SUBMITTING", "CANCEL_REQUESTED", "SUCCEEDED"),
        ("CREATED", "SUBMITTING", "CANCEL_REQUESTED", "RESULT_UNKNOWN", "RECONCILING", "CANCELLED"),
    ],
)
def test_business_paths_cover_sync_async_retry_cancel_and_reconciliation(path):
    for source, target in zip(path, path[1:], strict=False):
        validate_transition(source, target)
        assert STATUS_LABELS[GenerationStatus(source)]


def test_critical_safety_paths_are_explicit():
    for source in ("RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW", "SUCCEEDED", "CANCELLED"):
        for target in ("SUBMITTING", "CREATED"):
            with pytest.raises(DomainError):
                validate_transition(source, target)
    validate_transition("RESULT_UNKNOWN", "RECONCILING")
    validate_transition("FAILED", "RETRY_WAIT")
    validate_transition("RETRY_WAIT", "SUBMITTING")
    for state in ("QUEUED", "WAITING_REVIEW", "INVALID"):
        with pytest.raises(DomainError):
            validate_transition(state, "RUNNING")


def job_url(row):
    return f"/api/v1/projects/{row.project_id}/generation-jobs/{row.id}"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("CREATED", "CANCELLED"),
        ("RETRY_WAIT", "CANCELLED"),
        ("FAILED", "CANCELLED"),
        ("SUBMITTING", "CANCEL_REQUESTED"),
        ("RUNNING", "CANCEL_REQUESTED"),
        ("RESULT_UNKNOWN", "RESULT_UNKNOWN"),
        ("RECONCILING", "RECONCILING"),
        ("MANUAL_REVIEW", "MANUAL_REVIEW"),
    ],
)
def test_cancellation_is_durable_and_unknown_results_remain_uncertain(
    client, generation_seed, status, expected
):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed, status=status)
    response = client.post(job_url(row) + "/cancel", json={"expected_revision": 0})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == expected and data["revision"] == 1 and data["cancel_requested_at"]
    assert data["status_label"] == STATUS_LABELS[GenerationStatus(expected)]
    assert client.get(job_url(row)).json() == data
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 409
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 1}).json() == data
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "generation.cancel_requested")
            )
            == 1
        )


def test_transitions_persist_revision_and_audit_and_rollback_invalid_changes(
    client, generation_seed
):
    factory = client.app.state.session_factory
    with factory.begin() as db:
        row = add_job(db, generation_seed)
        job_id = row.id
        for revision, state in enumerate(
            ("SUBMITTING", "RESULT_UNKNOWN", "RECONCILING", "RUNNING", "SUCCEEDED")
        ):
            transition(db, job_id, GenerationStatus(state), revision)
    with pytest.raises(DomainError), factory.begin() as db:
        transition(db, job_id, GenerationStatus.RETRY_WAIT, 5)
    with factory() as db:
        assert find_job(db, job_id).status == "SUCCEEDED"
        assert find_job(db, job_id).revision == 5
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "generation.transitioned")
            )
            == 5
        )


def test_public_queries_are_bounded_and_cross_project_jobs_are_hidden(
    client, episode, generation_seed
):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
    base = job_url(row).rsplit("/", 1)[0]
    assert len(client.get(base, params={"limit": 1}).json()) == 1
    assert client.get(base, params={"offset": 1}).json() == []
    assert client.get(base, params={"limit": 1000}).status_code == 422
    other = client.post("/api/v1/projects", json={"name": "另一个项目"}).json()["id"]
    assert client.get(job_url(row).replace(episode["project_id"], other)).status_code == 404
    assert (
        client.post(
            job_url(row).replace(episode["project_id"], other) + "/cancel",
            json={"expected_revision": 0},
        ).status_code
        == 404
    )
    assert "request_fingerprint" not in client.get(job_url(row)).json()
    register(client, "outsider@example.com")
    assert client.get(base).status_code == 404
    assert client.get(job_url(row)).status_code == 404
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 404


def test_cancel_obeys_viewer_editor_archive_and_terminal_gates(client, generation_seed):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
    member = register(client, "media-member@example.com")
    client.headers["Authorization"] = "Bearer test-token"
    base = f"/api/v1/projects/{row.project_id}"
    membership = client.post(
        base + "/members", json={"email": "media-member@example.com", "role": "viewer"}
    )
    assert membership.status_code == 201, membership.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(job_url(row)).status_code == 200
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 403
    client.headers["Authorization"] = "Bearer test-token"
    assert (
        client.patch(base + "/members/" + member["user"]["id"], json={"role": "editor"}).status_code
        == 200
    )
    assert client.patch(base, json={"archived": True}).status_code == 200
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(job_url(row)).status_code == 200
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 409
    client.headers["Authorization"] = "Bearer test-token"
    assert client.patch(base, json={"archived": False}).status_code == 200
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 200


def test_cancellation_downgrade_cannot_discard_intent(client, generation_seed, migrated):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed)
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 200
    with pytest.raises(RuntimeError, match="cancellation history"):
        command.downgrade(migrated, "0027")
    assert client.get(job_url(row)).json()["status"] == "CANCELLED"


def test_success_cannot_be_cancelled_and_invalid_revisions_are_rejected(client, generation_seed):
    with client.app.state.session_factory.begin() as db:
        row = add_job(db, generation_seed, status="SUCCEEDED")
    assert client.post(job_url(row) + "/cancel", json={"expected_revision": 0}).status_code == 409
    for revision in (-1, True, "0"):
        assert (
            client.post(job_url(row) + "/cancel", json={"expected_revision": revision}).status_code
            == 422
        )
    client.headers.pop("Authorization")
    assert client.get(job_url(row)).status_code == 401


def test_postgres_stale_state_writers_have_one_winner(client, generation_seed, settings):
    require_postgres(settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        row = add_job(db, generation_seed)
        job_id = row.id
    barrier = Barrier(2)

    def change(_):
        try:
            with factory.begin() as db:
                db.get(GenerationJob, job_id)
                barrier.wait(timeout=15)
                transition(db, job_id, GenerationStatus.SUBMITTING, 0)
            return 200
        except DomainError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, range(2))) == [200, 409]
    with factory() as db:
        assert find_job(db, job_id).revision == 1


def test_postgres_cancel_and_worker_use_consistent_lock_order(client, generation_seed, settings):
    require_postgres(settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        row = add_job(db, generation_seed)
        job_id, project_id = row.id, row.project_id
    barrier = Barrier(2)

    def change(cancel):
        try:
            with factory.begin() as db:
                barrier.wait(timeout=15)
                if cancel:
                    require_project(db, project_id, "service-owner", write=True)
                    request_cancel(db, job_id, 0, "service-owner")
                else:
                    transition(db, job_id, GenerationStatus.SUBMITTING, 0)
            return 200
        except DomainError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(change, value) for value in (True, False)]
        assert sorted(item.result(timeout=20) for item in futures) == [200, 409]
