"""Retryable registration of native audio/video through the existing private file store."""

import hashlib
from copy import deepcopy
from uuid import UUID, uuid5

from sqlalchemy import exists, select
from sqlalchemy.orm import sessionmaker

from arclight.application.audit import audit
from arclight.application.dependencies import lock_dependency_graph
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.media.validation import probe
from arclight.persistence.models import (
    GenerationAttempt,
    GenerationJob,
    GenerationMediaArtifact,
    StoredFile,
)
from arclight.providers.media_outputs import MediaOutputProvider
from arclight.providers.media_submission import MediaSubmission
from arclight.providers.native_media import MAX_AUDIO_BYTES, MAX_VIDEO_BYTES, object_key
from arclight.providers.native_media_protocols import KINDS


def pending_attempts(factory: sessionmaker) -> list[UUID]:
    with factory() as db:
        return list(
            db.scalars(
                select(GenerationAttempt.id)
                .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
                .where(
                    GenerationJob.task_type.in_({"AUDIO", "VIDEO"}),
                    GenerationAttempt.provider.in_(KINDS),
                    GenerationAttempt.status == "SUCCEEDED",
                    ~exists().where(GenerationMediaArtifact.attempt_id == GenerationAttempt.id),
                )
                .order_by(GenerationAttempt.created_at)
                .limit(100)
            )
        )


def import_attempt(
    factory: sessionmaker, settings: Settings, attempt_id: UUID, provider: MediaOutputProvider
) -> int:
    with factory() as db:
        attempt = db.get(GenerationAttempt, attempt_id)
        if attempt is None or attempt.status != "SUCCEEDED" or not attempt.result_json:
            return 0
        job = db.get(GenerationJob, attempt.job_id)
        if job is None or job.task_type not in {"AUDIO", "VIDEO"} or provider.name != job.provider:
            return 0
        if job.provider not in KINDS:
            return 0
        if db.scalar(
            select(GenerationMediaArtifact.id).where(
                GenerationMediaArtifact.attempt_id == attempt_id
            )
        ):
            return 0
        request = MediaSubmission(
            job.id,
            attempt.id,
            attempt.input_manifest_id,
            deepcopy(attempt.input_snapshot_json),
            None,
        )
        handles = attempt.result_json["output_handles"]
        if len(handles) != 1:
            return 0
        handle = handles[0]
        task_type, project_id, creator = job.task_type, job.project_id, job.created_by
    maximum = MAX_AUDIO_BYTES if task_type == "AUDIO" else MAX_VIDEO_BYTES
    media, output, error = {}, None, None
    try:
        output = provider.download(request, handle, maximum)
        if not output.media_type.startswith("audio/" if task_type == "AUDIO" else "video/"):
            raise DomainError("INVALID_FILE", "结果类型与任务类型不一致", 422)
        media = probe(output.data, output.media_type)
    except DomainError as exc:
        if exc.status_code >= 500:
            return 0
        error = "INVALID_MEDIA_OUTPUT"
    except (TimeoutError, ConnectionError, OSError):
        return 0
    with factory.begin() as db:
        lock_dependency_graph(db, project_id)
        if db.scalar(
            select(GenerationMediaArtifact.id).where(
                GenerationMediaArtifact.attempt_id == attempt_id
            )
        ):
            return 0
        file_id = None
        if error is None:
            assert output is not None
            file_id = uuid5(attempt_id, "native-media:0")
            suffix = {
                "audio/wav": "wav",
                "audio/mpeg": "mp3",
                "audio/ogg": "ogg",
                "audio/flac": "flac",
                "video/mp4": "mp4",
            }[output.media_type]
            db.add(
                StoredFile(
                    id=file_id,
                    project_id=project_id,
                    storage_backend=settings.object_storage_backend.upper(),
                    bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
                    object_key=object_key(handle),
                    checksum=hashlib.sha256(output.data).hexdigest(),
                    byte_size=len(output.data),
                    media_type=output.media_type,
                    original_name=f"generated-{attempt_id}.{suffix}",
                    source_note=f"Generation attempt {attempt_id}",
                    rights_note="生成结果，授权待人工确认",
                    rights_confirmed=False,
                    created_by=creator,
                )
            )
            db.flush()
        db.add(
            GenerationMediaArtifact(
                job_id=request.job_id,
                attempt_id=attempt_id,
                ordinal=0,
                file_id=file_id,
                error_code=error,
                media_json=media,
            )
        )
        db.flush()
        audit(
            db,
            project_id,
            creator,
            "generation.media_registered",
            request.job_id,
            {
                "attempt_id": str(attempt_id),
                "file_id": str(file_id) if file_id else None,
                "error_code": error,
            },
        )
    return 1
