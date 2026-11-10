"""Render candidates share the conservative local/S3 retention and cleanup contracts."""

from datetime import timedelta
from uuid import UUID

import pytest
from render_job_helpers import job_case, read, render_case, review_case, run, timeline_case
from sqlalchemy.exc import SQLAlchemyError
from test_storage_cleanup import gc_setup, plan, run_item

from arclight.application import render_cleanup
from arclight.application.render_job_store import lock_job
from arclight.application.render_leases import claim, finish
from arclight.persistence.models import RenderAttempt, RenderExecution, StoredFile
from arclight.storage.cleanup import snapshot
from arclight.storage.objects import ObjectStorage

__all__ = ["gc_setup", "job_case", "render_case", "review_case", "timeline_case"]


@pytest.fixture
def storage_job(gc_setup, request):
    settings, store, _, clock = gc_setup
    manifest, value, _ = request.getfixturevalue("job_case")
    return settings, store, f"/api/v1/projects/{manifest['project_id']}", clock, value


def candidate(client, value):
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    token = claim(factory, run_id)
    assert token is not None
    with factory() as db:
        key = db.get(RenderAttempt, token).output_key
    return token, key


def failed(client, value):
    with client.app.state.session_factory.begin() as db:
        execution, job = lock_job(db, UUID(value["id"]))
        finish(db, execution, job, "FAILED", "RENDER_FAILED")


def test_active_candidate_and_every_input_survive_old_object_gc(client, storage_job):
    _, store, root, clock, value = storage_job
    _, key = candidate(client, value)
    store.put(key, b"in-progress-candidate", "video/mp4")
    clock[0] += timedelta(days=8)
    assert plan(client, root, "RENDERED")["items"] == []
    with client.app.state.session_factory() as db:
        source = db.get(StoredFile, db.get(RenderExecution, UUID(value["id"])).input_file_id)
        assert snapshot(store, source.object_key) is not None
    assert snapshot(store, key) is not None


def test_failed_candidate_retention_starts_at_finish_and_observes_before_delete(
    client, storage_job
):
    _, store, root, clock, value = storage_job
    token, key = candidate(client, value)
    store.put(key, b"abandoned", "video/mp4")
    failed(client, value)
    with client.app.state.session_factory() as db:
        from arclight.application.storage_cleanup import utc

        clock[0] = utc(db.get(RenderAttempt, token).finished_at) + timedelta(days=7, seconds=-1)
    assert plan(client, root, "RENDERED")["items"] == []
    clock[0] += timedelta(seconds=3)
    planned = plan(client, root, "RENDERED")
    assert len(planned["items"]) == 1 and planned["items"][0]["reason"] == "ORPHAN"
    assert run_item(client, root, planned).status_code == 409
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "DELETED"
    assert snapshot(store, key) is None
    render_cleanup.clean_candidates(
        client.app.state.session_factory, client.app.state.settings, UUID(value["id"])
    )
    assert read(client, value)["cleanup_status"] == "CLEANED"


def test_successful_output_and_inputs_survive_worker_cleanup_and_gc(client, storage_job):
    settings, store, root, clock, value = storage_job
    assert run(client, value) == "SUCCEEDED"
    render_cleanup.clean_candidates(client.app.state.session_factory, settings, UUID(value["id"]))
    assert read(client, value)["cleanup_status"] == "RETAINED"
    clock[0] += timedelta(days=365)
    assert plan(client, root, "RENDERED")["items"] == []
    with client.app.state.session_factory() as db:
        execution = db.get(RenderExecution, UUID(value["id"]))
        for file_id in (execution.input_file_id, execution.output_file_id):
            assert snapshot(store, db.get(StoredFile, file_id).object_key) is not None


def test_storage_identity_change_protects_failed_candidate_and_can_recover(client, storage_job):
    settings, store, root, clock, value = storage_job
    _, key = candidate(client, value)
    store.put(key, b"failed-candidate", "video/mp4")
    failed(client, value)
    original_path, original_bucket = settings.asset_storage_path, settings.s3_bucket
    if store.client is None:
        settings.asset_storage_path = original_path / "changed-identity"
    else:
        settings.s3_bucket += "-changed-identity"
    try:
        # Identity protection is evaluated before any destructive store operation.
        render_cleanup.clean_candidates(
            client.app.state.session_factory, settings, UUID(value["id"])
        )
        with client.app.state.session_factory() as db:
            attempt = db.get(RenderAttempt, UUID(key.split("/")[-1]))
            assert attempt.cleanup_status == "FAILED"
            assert attempt.cleanup_error_code == "RENDER_STORAGE_CHANGED"
    finally:
        settings.asset_storage_path, settings.s3_bucket = original_path, original_bucket
    assert snapshot(store, key) is not None
    render_cleanup.clean_candidates(client.app.state.session_factory, settings, UUID(value["id"]))
    assert snapshot(store, key) is None and read(client, value)["cleanup_status"] == "CLEANED"


def test_historical_file_receipt_protects_failed_candidate_from_worker_and_gc(client, storage_job):
    settings, store, root, clock, value = storage_job
    token, key = candidate(client, value)
    store.put(key, b"retained", "video/mp4")
    failed(client, value)
    with client.app.state.session_factory.begin() as db:
        execution = db.get(RenderExecution, UUID(value["id"]))
        db.add(
            StoredFile(
                project_id=execution.project_id,
                storage_backend=settings.object_storage_backend.upper(),
                bucket=settings.s3_bucket if store.client is not None else None,
                object_key=key,
                checksum="a" * 64,
                byte_size=8,
                media_type="video/mp4",
                original_name="historical-candidate",
                source_note="offline receipt protection test",
                rights_note="original fixture",
                rights_confirmed=True,
                created_by="service-owner",
            )
        )
    render_cleanup.clean_candidates(client.app.state.session_factory, settings, UUID(value["id"]))
    with client.app.state.session_factory() as db:
        assert db.get(RenderAttempt, token).cleanup_error_code == "RENDER_CLEANUP_PROTECTED"
    clock[0] += timedelta(days=8)
    assert plan(client, root, "RENDERED")["items"] == []
    assert snapshot(store, key) is not None


def test_cleanup_database_failure_preserves_retry_intent_without_native_diagnostics(
    client, storage_job, monkeypatch, caplog
):
    settings, store, _, _, value = storage_job
    token, key = candidate(client, value)
    store.put(key, b"retryable", "video/mp4")
    failed(client, value)

    def unavailable(*args):
        raise SQLAlchemyError("PRIVATE_DB_CREDENTIALS")

    with monkeypatch.context() as patch:
        patch.setattr(render_cleanup, "lock_job", unavailable)
        render_cleanup.clean_candidates(
            client.app.state.session_factory, settings, UUID(value["id"])
        )
    assert "DATABASE_UNAVAILABLE" in caplog.text and "PRIVATE_DB_CREDENTIALS" not in caplog.text
    with client.app.state.session_factory() as db:
        assert db.get(RenderAttempt, token).cleanup_status == "PENDING"
    assert snapshot(store, key) is not None
    render_cleanup.clean_candidates(client.app.state.session_factory, settings, UUID(value["id"]))
    assert snapshot(store, key) is None and read(client, value)["cleanup_status"] == "CLEANED"


def test_candidate_delete_failure_retries_against_real_store_without_reencoding(
    client, storage_job, monkeypatch, caplog
):
    settings, store, _, _, value = storage_job
    token, key = candidate(client, value)
    store.put(key, b"durable-failed-candidate", "video/mp4")
    failed(client, value)

    def unavailable(*args):
        raise OSError("PRIVATE_STORAGE_CREDENTIALS")

    with monkeypatch.context() as patch:
        patch.setattr(ObjectStorage, "delete", unavailable)
        render_cleanup.clean_candidates(
            client.app.state.session_factory, settings, UUID(value["id"])
        )
    with client.app.state.session_factory() as db:
        row = db.get(RenderAttempt, token)
        assert row.cleanup_status == "FAILED" and row.cleanup_error_code == "RENDER_CLEANUP_FAILED"
    assert snapshot(store, key) is not None
    assert "PRIVATE_STORAGE_CREDENTIALS" not in caplog.text
    assert run(client, value) == "FAILED"
    final = read(client, value)
    assert final["cleanup_status"] == "CLEANED" and final["attempt_count"] == 1
    assert snapshot(store, key) is None
