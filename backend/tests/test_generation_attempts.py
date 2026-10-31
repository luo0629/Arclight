"""GEN-03: submission receipts, definite outcomes and append-only explicit retries."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_assets import create
from test_concurrency import require_postgres
from test_generation_jobs import base, capture_ok, payload
from test_generation_schema import generation_seed as generation_seed

from arclight.application.generation_attempts import (
    finish_attempt,
    record_submission,
    start_attempt,
)
from arclight.application.generation_state import request_cancel, transition
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_requests import GenerationResult
from arclight.persistence.models import (
    DependencyManifest,
    Episode,
    GenerationAttempt,
    GenerationJob,
    Project,
    VisualAsset,
)


def start(client, job):
    with client.app.state.session_factory.begin() as db:
        return start_attempt(db, UUID(job["id"]), job["revision"])


def fail(client, job, attempt, error="PROVIDER_REJECTED"):
    with client.app.state.session_factory.begin() as db:
        finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            Status.FAILED,
            request_id=f"mock:{attempt.id}",
            error_code=error,
            latency_ms=120,
            cost=Decimal("0.003201"),
        )


def test_retry_appends_new_attempt_and_reuses_original_input_not_live_config(client, episode):
    project_id = episode["project_id"]
    body = payload()
    job = capture_ok(client, project_id, body)
    one = start(client, job)
    fail(client, job, one, error="PROVIDER_FAILED")
    url = base(project_id) + "/" + job["id"]
    first = client.get(url + f"/attempts/{one.id}").json()
    assert first["error_code"] == "PROVIDER_FAILED" and first["cost"] == "0.003201"
    assert first["provider_request_id"] == f"mock:{one.id}"
    assert first["submitted_at"].endswith("Z") and first["finished_at"].endswith("Z")
    failed = client.get(url).json()
    assert client.post(url + "/retry", json={"expected_revision": 0}).status_code == 409
    retry = client.post(url + "/retry", json={"expected_revision": failed["revision"]})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "RETRY_WAIT"
    body["spec"]["prompt"] = "当前配置已修改"
    body["spec"]["seed"] = 99
    two = start(client, retry.json())
    assert one.id != two.id and two.attempt_no == 2
    assert two.input_manifest_id == one.input_manifest_id
    assert two.input_snapshot_json == first["input_snapshot_json"]
    assert two.input_snapshot_json["context"]["generation"]["seed"] == 17
    with client.app.state.session_factory.begin() as db:
        record_submission(db, UUID(job["id"]), two.id, "mock:second")
    with client.app.state.session_factory.begin() as db:
        finish_attempt(
            db,
            UUID(job["id"]),
            two.id,
            Status.SUCCEEDED,
            result=GenerationResult(output_handles=["mock:output:2"], usage={"images": 1}),
            latency_ms=50,
        )
    attempts = client.get(url + "/attempts").json()
    assert [item["attempt_no"] for item in attempts] == [1, 2]
    assert attempts[0] == first
    assert attempts[1]["result_json"]["output_handles"] == ["mock:output:2"]
    assert attempts[1]["cost"] is None
    assert client.get(url + "/attempts?limit=1&offset=1").json() == [attempts[1]]
    assert client.get(url + "/attempts?limit=1000").status_code == 422
    assert client.get(url).json()["attempt_count"] == 2
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).provider_request_id == "mock:second"


@pytest.mark.parametrize(
    "status", ["CREATED", "SUBMITTING", "RESULT_UNKNOWN", "MANUAL_REVIEW", "CANCELLED", "SUCCEEDED"]
)
def test_retry_cannot_resubmit_unfinished_unknown_cancelled_or_successful_jobs(
    client, episode, status
):
    job = capture_ok(client, episode["project_id"])
    if status != "CREATED":
        attempt = start(client, job)
        with client.app.state.session_factory.begin() as db:
            if status in {"SUCCEEDED", "CANCELLED"}:
                if status == "CANCELLED":
                    request_cancel(db, UUID(job["id"]), 1, "service-owner")
                finish_attempt(
                    db,
                    UUID(job["id"]),
                    attempt.id,
                    Status(status),
                    result=GenerationResult(output_handles=["mock:one"])
                    if status == "SUCCEEDED"
                    else None,
                    error_code="CANCELLED" if status == "CANCELLED" else None,
                )
            elif status in {"RESULT_UNKNOWN", "MANUAL_REVIEW"}:
                transition(db, UUID(job["id"]), Status.RESULT_UNKNOWN, 1)
                if status == "MANUAL_REVIEW":
                    transition(db, UUID(job["id"]), Status.RECONCILING, 2)
                    transition(db, UUID(job["id"]), Status.MANUAL_REVIEW, 3)
    url = base(episode["project_id"]) + "/" + job["id"]
    before = client.get(url).json()
    assert (
        client.post(url + "/retry", json={"expected_revision": before["revision"]}).status_code
        == 409
    )
    assert client.get(url).json() == before


def test_submission_and_cancel_race_preserves_actual_success_and_request_id(client, episode):
    job = capture_ok(client, episode["project_id"])
    attempt = start(client, job)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        request_cancel(db, UUID(job["id"]), 1, "service-owner")
    with factory.begin() as db:
        record_submission(db, UUID(job["id"]), attempt.id, "mock:accepted")
        assert db.get(GenerationJob, UUID(job["id"])).status == "CANCEL_REQUESTED"
    with factory.begin() as db:
        finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            Status.SUCCEEDED,
            result=GenerationResult(output_handles=["mock:done"]),
        )
    with factory() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        assert row.status == "SUCCEEDED" and row.cancel_requested_at is not None
        assert db.get(GenerationAttempt, attempt.id).provider_request_id == "mock:accepted"


@pytest.mark.parametrize(
    "bad",
    [
        "request_id",
        "error",
        "negative_cost",
        "cost_precision",
        "latency",
        "outcome",
        "missing_result",
        "unknown_error",
    ],
)
def test_invalid_receipts_are_rejected_without_partial_writes(client, episode, bad):
    job = capture_ok(client, episode["project_id"])
    attempt = start(client, job)
    kwargs = {"error_code": "PROVIDER_FAILED"}
    status = Status.FAILED
    if bad == "request_id":
        kwargs["request_id"] = "Bearer secret-canary"
    elif bad == "error":
        kwargs["error_code"] = "raw secret-canary exception"
    elif bad == "negative_cost":
        kwargs["cost"] = Decimal("-1")
    elif bad == "cost_precision":
        kwargs["cost"] = Decimal("0.0000001")
    elif bad == "latency":
        kwargs["latency_ms"] = True
    elif bad == "outcome":
        status = Status.RESULT_UNKNOWN
    elif bad == "unknown_error":
        kwargs["error_code"] = "RESULT_UNKNOWN"
    else:
        status = Status.SUCCEEDED
        kwargs.clear()
    with pytest.raises(DomainError), client.app.state.session_factory.begin() as db:
        finish_attempt(db, UUID(job["id"]), attempt.id, status, **kwargs)
    with client.app.state.session_factory() as db:
        row = db.get(GenerationAttempt, attempt.id)
        assert row.status == "SUBMITTING" and row.provider_request_id is None
        assert row.error_code is None and row.finished_at is None


def test_finished_and_foreign_attempts_cannot_be_rebound(client, episode):
    job = capture_ok(client, episode["project_id"])
    other = capture_ok(client, episode["project_id"])
    attempt = start(client, job)
    factory = client.app.state.session_factory
    with pytest.raises(DomainError), factory.begin() as db:
        record_submission(db, UUID(other["id"]), attempt.id, "mock:wrong")
    with factory.begin() as db:
        record_submission(db, UUID(job["id"]), attempt.id, "mock:original")
    with pytest.raises(DomainError), factory.begin() as db:
        finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            Status.FAILED,
            request_id="mock:replacement",
            error_code="PROVIDER_FAILED",
        )
    with factory.begin() as db:
        finish_attempt(db, UUID(job["id"]), attempt.id, Status.FAILED, error_code="PROVIDER_FAILED")
    with pytest.raises(DomainError), factory.begin() as db:
        finish_attempt(db, UUID(job["id"]), attempt.id, Status.FAILED, error_code="INPUT_INVALID")
    url = base(episode["project_id"]) + "/" + other["id"]
    assert client.get(url + f"/attempts/{attempt.id}").status_code == 404


def test_legacy_manifest_is_not_silently_upgraded_at_submission(client, generation_seed):
    from test_generation_schema import add_job

    factory = client.app.state.session_factory
    with factory.begin() as db:
        job = add_job(db, generation_seed)
        before = deepcopy(db.get(DependencyManifest, job.input_manifest_id).snapshot_json)
    with pytest.raises(DomainError) as raised, factory.begin() as db:
        start_attempt(db, job.id, 0)
    assert raised.value.code == "INVALID_GENERATION_INPUT"
    with factory() as db:
        assert db.get(GenerationJob, job.id).status == "CREATED"
        assert db.get(DependencyManifest, job.input_manifest_id).snapshot_json == before


@pytest.mark.parametrize(
    "bad",
    [
        {"output_handles": []},
        {"output_handles": ["https://provider/result?token=secret"]},
        {"output_handles": ["mock:one"], "metadata": {"api_key": "secret"}},
    ],
)
def test_results_accept_only_bounded_object_handles_and_safe_metadata(bad):
    with pytest.raises(ValidationError):
        GenerationResult.model_validate(bad)


@pytest.mark.parametrize("gate", ["project", "episode", "target"])
@pytest.mark.parametrize("retry", [False, True])
def test_submission_and_retry_obey_current_archive_and_lock_gates(client, episode, gate, retry):
    target = create(client, episode["project_id"])
    job = capture_ok(client, episode["project_id"], payload(target, episode_id=episode["id"]))
    if retry:
        fail(client, job, start(client, job))
    with client.app.state.session_factory.begin() as db:
        if gate == "project":
            db.get(Project, UUID(episode["project_id"])).archived = True
        elif gate == "episode":
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
        else:
            db.get(VisualAsset, UUID(target["id"])).status = "LOCKED"
    if retry:
        url = base(episode["project_id"]) + "/" + job["id"]
        current = client.get(url).json()
        assert (
            client.post(url + "/retry", json={"expected_revision": current["revision"]}).status_code
            == 409
        )
    else:
        with pytest.raises(DomainError):
            start(client, job)


def test_postgres_concurrent_submit_and_finish_each_have_one_winner(client, episode, settings):
    require_postgres(settings)
    job = capture_ok(client, episode["project_id"])
    factory = client.app.state.session_factory

    def race(finish=False):
        barrier = Barrier(2)

        def write(_):
            try:
                with factory.begin() as db:
                    barrier.wait(timeout=15)
                    if finish:
                        row = db.scalar(
                            select(GenerationAttempt).where(
                                GenerationAttempt.job_id == UUID(job["id"])
                            )
                        )
                        finish_attempt(
                            db, UUID(job["id"]), row.id, Status.FAILED, error_code="PROVIDER_FAILED"
                        )
                    else:
                        start_attempt(db, UUID(job["id"]), 0)
                return 200
            except DomainError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(write, range(2))) == [200, 409]

    race()
    race(finish=True)
    with factory() as db:
        assert len(list(db.scalars(select(GenerationAttempt)))) == 1
        row = db.get(GenerationJob, UUID(job["id"]))
        assert row.attempt_count == 1 and row.status == "FAILED"
