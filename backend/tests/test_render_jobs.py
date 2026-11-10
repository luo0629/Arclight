"""Render lifecycle exercises real media and the authorized, durable API path."""

from io import BytesIO
from uuid import UUID

import av
from render_job_helpers import job_case, read, render_case, review_case, run, submit, timeline_case
from sqlalchemy import func, select

from arclight.application import render_execution
from arclight.persistence.models import RenderAttempt, RenderExecution, StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "review_case", "timeline_case", "job_case"]


def test_real_manifest_render_is_decodable_exact_and_idempotent(client, job_case):
    manifest, value, body = job_case
    response = client.post(f"/api/v1/render-manifests/{manifest['id']}/jobs", json=body)
    assert response.status_code == 202 and response.json() == value
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    assert final["progress"] == 100 and final["phase"] == "SUCCEEDED"
    assert final["cleanup_status"] == "RETAINED" and not final["is_delivery"]
    assert final["attempt_count"] == 1 and final["manifest_sha256"] == manifest["content_sha256"]
    assert run(client, value) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        output = db.get(StoredFile, UUID(final["output_file_id"]))
        movie = ObjectStorage(client.app.state.settings).read(
            output.object_key, 128 * 1024**2, output.checksum
        )
        assert db.scalar(select(func.count(RenderAttempt.id))) == 1
        assert db.scalar(select(func.count(RenderExecution.id))) == 1
    with av.open(BytesIO(movie)) as container:
        video, audio = container.streams.video[0], container.streams.audio[0]
        assert video.codec_context.name == "h264" and video.codec_context.pix_fmt == "yuv420p"
        assert (video.width, video.height, video.average_rate) == (256, 256, 24)
        assert audio.codec_context.name == "aac" and audio.sample_rate == 48000
        frames = list(container.decode(video=0))
        assert len(frames) == 60
        assert float(video.duration * video.time_base) == 2.5
    with av.open(BytesIO(movie)) as container:
        assert sum(frame.samples for frame in container.decode(audio=0)) >= 120000
    history = client.get(f"/api/v1/render-jobs/{value['id']}/attempts")
    assert history.status_code == 200 and history.json()[0]["status"] == "SUCCEEDED"
    assert "output_key" not in history.text and "lease_token" not in history.text


def test_failed_render_retries_as_new_job_and_preserves_history_and_inputs(
    client, job_case, monkeypatch
):
    manifest, value, _ = job_case
    factory, settings = client.app.state.session_factory, client.app.state.settings
    with factory() as db:
        sources = list(db.scalars(select(StoredFile)))
        original_inputs = {
            row.id: ObjectStorage(settings).read(row.object_key, row.byte_size, row.checksum)
            for row in sources
        }

    def fail(*args, **kwargs):
        raise RuntimeError("injected encoder failure")

    with monkeypatch.context() as patch:
        patch.setattr(render_execution, "execute_media", fail)
        assert run(client, value) == "FAILED"
    failed = read(client, value)
    history_url = f"/api/v1/render-jobs/{value['id']}/attempts"
    failed_history = client.get(history_url).json()
    retry, _ = submit(client, manifest, retry_of=value["id"])
    assert run(client, retry) == "SUCCEEDED"
    final = read(client, retry)
    assert final["retry_of_id"] == value["id"] and final["id"] != value["id"]
    assert final["output_file_id"] and failed["output_file_id"] is None
    assert read(client, value) == failed and client.get(history_url).json() == failed_history
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    with factory() as db:
        assert db.scalar(select(func.count(RenderExecution.id))) == 2
        assert db.scalar(select(func.count(RenderAttempt.id))) == 2
        for row in sources:
            assert (
                ObjectStorage(settings).read(row.object_key, row.byte_size, row.checksum)
                == (original_inputs[row.id])
            )
        output = db.get(StoredFile, UUID(final["output_file_id"]))
        movie = ObjectStorage(settings).read(output.object_key, output.byte_size, output.checksum)
    with av.open(BytesIO(movie)) as container:
        assert len(list(container.decode(video=0))) == 60
