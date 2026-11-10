"""Failures leave safe, recoverable receipts and cannot publish stale candidates."""

from uuid import UUID

import pytest
from render_job_helpers import (
    cancel,
    expire,
    job_case,
    read,
    render_case,
    review_case,
    run,
    submit,
    timeline_case,
)
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from test_collaboration import register
from test_media_execution import recorded_children

from arclight.application import render_execution
from arclight.application.render_dispatch import candidates
from arclight.application.render_leases import claim
from arclight.media.execution import MediaExecutionError
from arclight.persistence.models import (
    AssetVersion,
    Project,
    ProjectMember,
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    StoredFile,
)
from arclight.storage.cleanup import snapshot
from arclight.storage.objects import ObjectStorage

__all__ = ["job_case", "render_case", "review_case", "timeline_case", "recorded_children"]
PRIVATE = "PRIVATE_NATIVE_ERROR_TOKEN:/private/input?signature=secret"


def attempt(client, value):
    with client.app.state.session_factory() as db:
        return db.scalar(select(RenderAttempt).where(RenderAttempt.run_id == UUID(value["id"])))


@pytest.mark.parametrize("failure", ["encoder", "object_write", "database_commit"])
def test_failed_candidate_write_or_commit_is_cleaned_and_diagnostics_are_safe(
    client, job_case, monkeypatch, caplog, failure
):
    _, value, _ = job_case
    factory, settings = client.app.state.session_factory, client.app.state.settings
    original_put = ObjectStorage.put
    real_encode = render_execution.execute_media
    written = []

    def encode(*args, **kwargs):
        if failure == "encoder":
            raise RuntimeError(PRIVATE)
        return real_encode(*args, **kwargs)

    def put(self, key, data, media_type):
        result = original_put(self, key, data, media_type)
        if key.startswith("rendered/"):
            written.append(key)
            if failure == "object_write":
                raise OSError(PRIVATE)
        return result

    injected = []

    def fail_commit(db):
        row = db.get(RenderExecution, UUID(value["id"]))
        if failure == "database_commit" and row.status == "SUCCEEDED" and not injected:
            injected.append(True)
            raise SQLAlchemyError(PRIVATE)

    monkeypatch.setattr(render_execution, "execute_media", encode)
    monkeypatch.setattr(ObjectStorage, "put", put)
    event.listen(factory.class_, "before_commit", fail_commit)
    try:
        assert run(client, value) == "FAILED"
    finally:
        event.remove(factory.class_, "before_commit", fail_commit)
    final = read(client, value)
    assert final["error_code"] == "RENDER_FAILED" and final["output_file_id"] is None
    assert final["cleanup_status"] == "CLEANED" and final["finished_at"]
    assert (len(written) == 1) == (failure != "encoder")
    assert snapshot(ObjectStorage(settings), attempt(client, value).output_key) is None
    with factory() as db:
        receipt = db.scalar(select(RenderInspection))
        assert (receipt is not None) == (failure != "encoder")
        if receipt is not None:
            assert receipt.passed and receipt.attempt_id == attempt(client, value).id
        output_key = attempt(client, value).output_key
        assert not db.scalar(select(StoredFile.id).where(StoredFile.object_key == output_key))
        assert not db.scalar(
            select(RenderArtifact.id).where(RenderArtifact.job_id == UUID(value["id"]))
        )
        assert not db.scalar(
            select(AssetVersion.id).where(AssetVersion.creation_key == UUID(value["id"]))
        )
        source = db.get(StoredFile, db.get(RenderExecution, UUID(value["id"])).input_file_id)
        assert snapshot(ObjectStorage(settings), source.object_key) is not None
    history = client.get(f"/api/v1/render-jobs/{value['id']}/attempts")
    assert history.json()[0]["status"] == "FAILED"
    if failure != "encoder":
        assert history.json()[0]["inspection"]["passed"]
        assert history.json()[0]["inspection"]["error_code"] is None
    assert PRIVATE not in history.text + str(final) + caplog.text


def test_cleanup_failure_is_durable_and_dispatcher_retries_without_reencoding(
    client, job_case, monkeypatch, caplog
):
    _, value, _ = job_case
    factory = client.app.state.session_factory

    def encode(*args, **kwargs):
        raise MediaExecutionError("TIMEOUT")

    def refuse(*args, **kwargs):
        raise OSError(PRIVATE)

    monkeypatch.setattr(render_execution, "execute_media", encode)
    with monkeypatch.context() as patch:
        patch.setattr(ObjectStorage, "delete", refuse)
        assert run(client, value) == "FAILED"
    final = read(client, value)
    assert final["error_code"] == "RENDER_MEDIA_TIMEOUT" and final["cleanup_status"] == "FAILED"
    assert UUID(value["id"]) in candidates(factory)
    assert attempt(client, value).cleanup_error_code == "RENDER_CLEANUP_FAILED"
    assert run(client, value) == "FAILED"
    assert read(client, value)["cleanup_status"] == "CLEANED"
    assert UUID(value["id"]) not in candidates(factory)
    assert read(client, value)["attempt_count"] == 1
    assert PRIVATE not in caplog.text


def test_running_cancel_terminates_and_reaps_actual_media_child(
    client, job_case, monkeypatch, recorded_children
):
    _, value, _ = job_case
    capture, children, _, _ = recorded_children
    real_encode = render_execution.execute_media

    def encode(*args, **kwargs):
        capture("import sys,time; sys.stdin.buffer.read(); time.sleep(30)")
        observer = kwargs["observer"]
        cancelled = []

        def observe(progress):
            if not cancelled:
                response = cancel(client, value)
                assert response.status_code == 200 and response.json()["status"] == "RUNNING"
                cancelled.append(True)
            observer(progress)

        return real_encode(*args, **{**kwargs, "observer": observe})

    monkeypatch.setattr(render_execution, "execute_media", encode)
    assert run(client, value) == "CANCELLED"
    final = read(client, value)
    assert final["output_file_id"] is None and final["error_code"] is None
    assert final["cleanup_status"] == "CLEANED"
    assert children and all(child.poll() is not None for child in children)
    assert children[-1].returncode != 0


def test_stale_encoder_cannot_publish_or_finish_replacement_attempt(client, job_case, monkeypatch):
    _, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    replacement = []

    def encode(*args, **kwargs):
        expire(client, value)
        replacement.append(claim(factory, run_id))
        return b"stale-worker-output"

    monkeypatch.setattr(render_execution, "execute_media", encode)
    assert run(client, value) == "RUNNING"
    final = read(client, value)
    assert final["attempt_count"] == 2 and final["output_file_id"] is None
    with factory() as db:
        job = db.get(RenderJob, run_id)
        assert job.lease_token == replacement[0]
        rows = list(db.scalars(select(RenderAttempt).order_by(RenderAttempt.attempt_no)))
        assert [row.status for row in rows] == ["INTERRUPTED", "RUNNING"]
        assert rows[0].cleanup_status == "CLEANED" and rows[1].cleanup_status == "PENDING"


@pytest.mark.parametrize(
    "problem,code",
    [
        ("project", "PROJECT_ARCHIVED"),
        ("stale", "ASSET_NOT_USABLE"),
        ("object", "OBJECT_UNAVAILABLE"),
    ],
)
def test_completion_rechecks_scope_and_input_after_encoding(
    client, job_case, monkeypatch, problem, code
):
    manifest, value, _ = job_case

    def encode(*args, **kwargs):
        with client.app.state.session_factory.begin() as db:
            if problem == "project":
                db.get(Project, UUID(manifest["project_id"])).archived = True
            elif problem == "stale":
                source = next(
                    item
                    for item in manifest["snapshot"]["sources"]
                    if item["asset_type"] in {"IMAGE", "SHOT_IMAGE"}
                )
                db.get(AssetVersion, UUID(source["asset_version_id"])).stale = True
            else:
                source = db.get(
                    StoredFile, db.get(RenderExecution, UUID(value["id"])).input_file_id
                )
                ObjectStorage(client.app.state.settings).delete(source.object_key)
        return b"candidate-never-persisted"

    monkeypatch.setattr(render_execution, "execute_media", encode)
    assert run(client, value) == "FAILED"
    final = read(client, value)
    assert final["error_code"] == code and final["output_file_id"] is None
    assert final["cleanup_status"] == "CLEANED"


@pytest.mark.parametrize("revoke_at", ["queued", "encoding"])
def test_worker_rechecks_creator_permission_before_execution_and_completion(
    client, job_case, monkeypatch, revoke_at
):
    manifest, _, _ = job_case
    member = register(client, "render-revoked-editor@example.com")
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(manifest["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="editor",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    value, _ = submit(client, manifest)

    def revoke():
        with client.app.state.session_factory.begin() as db:
            db.get(ProjectMember, membership_id).role = "viewer"

    encodes = []

    def encode(*args, **kwargs):
        encodes.append(True)
        revoke()
        return b"candidate-never-persisted"

    if revoke_at == "queued":
        revoke()
    monkeypatch.setattr(render_execution, "execute_media", encode)
    assert run(client, value) == "FAILED"
    client.headers["Authorization"] = "Bearer test-token"
    final = read(client, value)
    assert final["error_code"] == "FORBIDDEN" and final["output_file_id"] is None
    assert final["cleanup_status"] == "CLEANED"
    assert bool(encodes) == (revoke_at == "encoding")
