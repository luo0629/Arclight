from datetime import timedelta
from io import BytesIO
from uuid import UUID, uuid4

import av
import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_file_transfers import mint_image
from test_storage_cleanup import gc_setup, plan, run_item

from arclight.application import rendering
from arclight.core.errors import DomainError
from arclight.persistence.models import RenderExecution, StoredFile
from arclight.storage.cleanup import snapshot

__all__ = ["gc_setup"]


def new_render(client, settings, root, file_id, retry_of=None):
    response = client.post(
        root + f"/files/{file_id}/render-previews",
        json={"retry_of": str(retry_of) if retry_of else None},
    )
    assert response.status_code == 202, response.text
    assert "intermediate_key" not in response.text
    run_id = UUID(response.json()["id"])
    with client.app.state.session_factory() as db:
        row = db.get(RenderExecution, run_id)
        return row.id, row.intermediate_key


def test_real_ffmpeg_intermediate_active_terminal_and_final_reference_lifecycle(
    client, gc_setup, monkeypatch
):
    settings, store, root, clock = gc_setup
    source = mint_image(client, root)
    run_id, intermediate = new_render(client, settings, root, source["id"])
    actual_phase = rendering.render_phase

    def phase(data, mode):
        if mode == "finalize":
            assert snapshot(store, intermediate) is not None
            saved_clock = clock[0]
            clock[0] += timedelta(days=8)
            assert plan(client, root, "INTERMEDIATE")["items"] == []
            clock[0] = saved_clock
        return actual_phase(data, mode)

    monkeypatch.setattr(rendering, "render_phase", phase)
    from arclight.workers import tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    published = []
    monkeypatch.setattr(
        tasks.execute_render_preview, "apply_async", lambda args: published.append(args)
    )
    tasks.dispatch_outbox.run()
    assert published == [[str(run_id)]]
    assert tasks.execute_render_preview.run(str(run_id)) == "SUCCEEDED"
    assert (
        rendering.execute_preview(client.app.state.session_factory, settings, run_id) == "SUCCEEDED"
    )
    response = client.get(root + f"/render-previews/{run_id}")
    assert response.json()["status"] == "SUCCEEDED"
    assert "object_key" not in response.text and "intermediate_key" not in response.text
    with client.app.state.session_factory() as db:
        run = db.get(RenderExecution, run_id)
        assert run.finished_at is not None and run.error_code is None
        output = db.get(StoredFile, run.output_file_id)
        assert output.project_id == UUID(root.split("/")[-1])
        assert output.media_type == "video/mp4"
        assert output.rights_confirmed and output.asset_version_id is None
        movie = store.read(output.object_key, 1024 * 1024, output.checksum)
        output_key = output.object_key
        assert db.scalar(select(RenderExecution.id).where(RenderExecution.id == run_id)) == run_id
    with av.open(BytesIO(movie), mode="r") as container:
        frames = list(container.decode(video=0))
        assert len(frames) == 24 and (frames[0].width, frames[0].height) == (256, 256)
        assert (
            float(container.streams.video[0].duration * container.streams.video[0].time_base) == 1
        )
    clock[0] += timedelta(hours=23)
    assert plan(client, root, "INTERMEDIATE")["items"] == []
    clock[0] += timedelta(hours=2)
    planned = plan(client, root, "INTERMEDIATE")
    assert len(planned["items"]) == 1
    assert planned["items"][0]["reason"] == "FINISHED_INTERMEDIATE"
    assert run_item(client, root, planned).status_code == 409
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "DELETED"
    assert snapshot(store, intermediate) is None
    clock[0] += timedelta(days=8)
    assert plan(client, root, "RENDERED")["items"] == []
    assert store.read(output_key, 1024 * 1024) == movie
    assert client.get(root + f"/files/{source['id']}").status_code == 200
    assert run_item(client, root, planned).json()["attempt_count"] == 1


def test_real_renderer_failure_retains_intermediate_diagnostic_and_explicit_retry(
    client, gc_setup, monkeypatch, migrated
):
    settings, store, root, clock = gc_setup
    source = mint_image(client, root)
    failed_id, failed_key = new_render(client, settings, root, source["id"])
    actual_phase = rendering.render_phase

    def fail_finalization(data, mode):
        if mode == "finalize":
            raise DomainError("RENDER_FAILED", "private stderr is never persisted", 422)
        return actual_phase(data, mode)

    monkeypatch.setattr(rendering, "render_phase", fail_finalization)
    assert (
        rendering.execute_preview(client.app.state.session_factory, settings, failed_id) == "FAILED"
    )
    with client.app.state.session_factory() as db:
        row = db.get(RenderExecution, failed_id)
        assert row.error_code == "RENDER_FAILED" and row.output_file_id is None
        assert row.finished_at is not None
    assert snapshot(store, failed_key) is not None
    assert plan(client, root, "INTERMEDIATE")["items"] == []
    monkeypatch.setattr(rendering, "render_phase", actual_phase)
    retry_id, retry_key = new_render(client, settings, root, source["id"], retry_of=failed_id)
    assert retry_id != failed_id and retry_key != failed_key
    assert (
        rendering.execute_preview(client.app.state.session_factory, settings, retry_id)
        == "SUCCEEDED"
    )
    clock[0] += timedelta(days=2)
    planned = plan(client, root, "INTERMEDIATE")
    assert len(planned["items"]) == 2
    clock[0] += timedelta(hours=2)
    for item in planned["items"]:
        assert run_item(client, root, planned, item).json()["status"] == "DELETED"
    assert snapshot(store, failed_key) is None and snapshot(store, retry_key) is None
    for sql in (
        "UPDATE render_executions SET input_file_id=id",
        "DELETE FROM render_executions",
    ):
        with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
            db.execute(text(sql))
    with pytest.raises(RuntimeError, match="Cannot remove saved renderer execution history"):
        command.downgrade(migrated, "0031")


def test_renderer_checks_project_scope_and_current_permission(client, gc_setup):
    settings, _, root, _ = gc_setup
    source = mint_image(client, root)
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError):
            rendering.create_preview(db, settings, uuid4(), UUID(source["id"]), "service-owner")
        with pytest.raises(DomainError):
            rendering.create_preview(
                db, settings, UUID(root.split("/")[-1]), uuid4(), "service-owner"
            )
    run_id, _ = new_render(client, settings, root, source["id"])
    assert client.patch(root, json={"archived": True}).status_code == 200
    assert rendering.execute_preview(client.app.state.session_factory, settings, run_id) == "FAILED"
    with client.app.state.session_factory() as db:
        assert db.get(RenderExecution, run_id).error_code == "PROJECT_ARCHIVED"


def test_preview_api_roles_revocation_and_project_isolation(client, gc_setup):
    from test_collaboration import create_project, register

    settings, _, _, _ = gc_setup
    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    source = mint_image(client, root)
    run_id, _ = new_render(client, settings, root, source["id"])
    viewer = register(client, "render-viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "render-viewer@example.com", "role": "viewer"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert client.get(root + f"/render-previews/{run_id}").status_code == 200
    assert client.post(root + f"/files/{source['id']}/render-previews", json={}).status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    other = create_project(client)
    assert client.get(f"/api/v1/projects/{other['id']}/render-previews/{run_id}").status_code == 404
    assert client.delete(root + f"/members/{viewer['user']['id']}").status_code == 204
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert client.get(root + f"/render-previews/{run_id}").status_code == 404
