"""Offline VIDEO-01 fixtures restricted to the browser runner's temporary database."""

import io
import logging
import tempfile
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import av
import httpx
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.generation_artifacts import import_attempt
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_reconciliation import mark_result_unknown, reconcile
from arclight.application.media_services import connection_for_job
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.models import (
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
)
from arclight.providers.native_media import NativeMediaProvider

MODEL = "doubao-seedance-1-0-pro-250528"
BASE_URL = "https://native.example/v1"
LOGGER = logging.getLogger("arclight.e2e_video")


class SyntheticVideoFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def isolated_browser_database(factory: sessionmaker[Session], settings: Settings) -> bool:
    if settings.environment != "test":
        return False
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return False
    path = Path(url.database).resolve()
    bind = factory.kw.get("bind")
    return (
        path.name == "business.db"
        and path.parent.name.startswith("arclight-e2e-")
        and path.parent.parent == Path(tempfile.gettempdir()).resolve()
        and bind is not None
        and bind.url == url
    )


@lru_cache(maxsize=1)
def synthetic_video() -> bytes:
    """A playable H.264 clip; deterministic media checks remain the production ones."""
    data = io.BytesIO()
    with av.open(data, "w", format="mp4") as output:
        stream = output.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 1280, 720, "yuv420p"
        stream.options = {"preset": "ultrafast", "crf": "30", "profile": "baseline"}
        stream.codec_context.thread_count = 1
        for index in range(144):
            frame = av.VideoFrame(1280, 720, "yuv420p")
            for number, plane in enumerate(frame.planes):
                plane.update(bytes([32 + index % 128 if number == 0 else 128]) * plane.buffer_size)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return data.getvalue()


def _advance(factory: sessionmaker[Session], settings: Settings, job_id: UUID) -> int:
    request_id = "video01-" + job_id.hex

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "native.example" and request.method == "POST":
            return httpx.Response(200, json={"id": request_id})
        if request.url.host == "native.example" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "content": {"video_url": "https://video01.example/" + request_id + ".mp4"},
                },
            )
        if request.url.host == "video01.example" and request.method == "GET":
            return httpx.Response(
                200, content=synthetic_video(), headers={"content-type": "video/mp4"}
            )
        return httpx.Response(400, json={"error": {"code": "SYNTHETIC_ROUTE_REFUSED"}})

    with factory() as db:
        job = db.get(GenerationJob, job_id)
        assert job is not None
        manifest = db.get(DependencyManifest, job.input_manifest_id)
        assert manifest is not None
        output = manifest.snapshot_json["context"]["generation"]["output_spec"]
        if any(
            output.get(name) != value
            for name, value in (
                ("width", 1280),
                ("height", 720),
                ("duration_seconds", 6),
                ("format", "mp4"),
            )
        ):
            raise SyntheticVideoFailure("SYNTHETIC_VIDEO_SPEC_REFUSED")
        connection = connection_for_job(db, job, settings)
        if connection is None or any(
            connection.configuration.get(name) != value
            for name, value in (("protocol", "seedance"), ("model", MODEL), ("base_url", BASE_URL))
        ):
            raise SyntheticVideoFailure("SYNTHETIC_VIDEO_CONFIGURATION_REFUSED")
        dispatch = db.scalar(
            select(GenerationDispatch).where(
                GenerationDispatch.job_id == job.id,
                GenerationDispatch.status == "READY",
                GenerationDispatch.expected_revision == job.revision,
            )
        )
        if dispatch is None:
            return 0
        event_id, project_id, actor = dispatch.id, job.project_id, job.created_by
    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(respond))
    status = consume_dispatch(factory, job_id, event_id, provider)
    if status != "SUBMITTED":
        raise SyntheticVideoFailure("SYNTHETIC_VIDEO_DISPATCH_" + status)
    with factory.begin() as db:
        attempt = db.scalar(
            select(GenerationAttempt)
            .where(
                GenerationAttempt.job_id == job_id,
            )
            .order_by(GenerationAttempt.attempt_no.desc())
            .limit(1)
        )
        assert attempt is not None
        attempt_id = attempt.id
        job = mark_result_unknown(db, job_id, attempt_id, request_id=request_id)
        revision = job.revision
    job = reconcile(factory, project_id, job_id, revision, actor, provider)
    if job.status != "SUCCEEDED":
        raise SyntheticVideoFailure("SYNTHETIC_VIDEO_RECONCILIATION_" + job.status)
    return import_attempt(factory, settings, attempt_id, provider)


def advance_videos(factory: sessionmaker[Session], settings: Settings) -> int:
    """Advance only explicitly marked CREATED jobs; never issue review or selection authority."""
    if not isolated_browser_database(factory, settings):
        return 0
    try:
        with factory() as db:
            pending = list(
                db.scalars(
                    select(GenerationJob.id)
                    .join(
                        DependencyManifest,
                        GenerationJob.input_manifest_id == DependencyManifest.id,
                    )
                    .where(
                        GenerationJob.status == "CREATED",
                        GenerationJob.target_type == "VIDEO",
                        GenerationJob.task_type == "VIDEO",
                        GenerationJob.provider == "seedance",
                        GenerationJob.model == MODEL,
                        DependencyManifest.snapshot_json["context"]["generation"]["context"][
                            "e2e_synthetic_video"
                        ].as_string()
                        == "video01",
                    )
                    .limit(10)
                )
            )
    except Exception as error:
        LOGGER.error("Synthetic video queue failed: %s", type(error).__name__)
        return 0
    imported = 0
    for job_id in pending:
        try:
            imported += _advance(factory, settings, job_id)
        except Exception as error:
            # Keep the browser worker alive and retain a safe, explicit diagnostic.
            code = (
                error.code
                if isinstance(error, (DomainError, SyntheticVideoFailure))
                else type(error).__name__
            )
            LOGGER.error("Synthetic video job %s failed: %s", job_id, code)
    return imported
