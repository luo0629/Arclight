"""Deterministic still-preview producer and durable intermediate lifecycle.

This is the concrete storage/renderer integration. Timeline composition, audio
and production UI remain in their own RENDER tasks.
"""

import hashlib
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.commands import require_project
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.validation import validate_file
from arclight.persistence.base import utcnow
from arclight.persistence.models import Project, RenderExecution, RenderJob, StoredFile
from arclight.storage.cleanup import identity
from arclight.storage.objects import ObjectStorage


def create_preview(
    db: Session,
    settings: Settings,
    project_id: UUID,
    file_id: UUID,
    actor: str,
    *,
    retry_of: UUID | None = None,
) -> RenderExecution:
    require_project(db, project_id, actor, write=True)
    source = db.get(StoredFile, file_id)
    if source is None or source.project_id != project_id:
        raise DomainError("NOT_FOUND", "渲染来源文件不存在", 404)
    if source.media_type not in {"image/png", "image/jpeg"} or not source.rights_confirmed:
        raise DomainError("RENDER_INPUT_INVALID", "预览需要已登记且声明使用权的图片", 409)
    if retry_of is not None:
        previous = db.get(RenderExecution, retry_of)
        if (
            previous is None
            or previous.project_id != project_id
            or previous.input_file_id != file_id
            or db.get(RenderJob, retry_of) is not None
        ):
            raise DomainError("NOT_FOUND", "原渲染任务不存在", 404)
        if previous.status != "FAILED":
            raise DomainError("RENDER_RETRY_CONFLICT", "只能另建失败任务的重试", 409)
    run_id = uuid4()
    row = RenderExecution(
        id=run_id,
        project_id=project_id,
        input_file_id=file_id,
        retry_of_id=retry_of,
        created_by=actor,
        storage_identity=identity(ObjectStorage(settings)),
        intermediate_key=f"intermediate/{project_id}/{run_id}/{uuid4()}",
    )
    db.add(row)
    db.flush()
    return row


def render_phase(data: bytes, phase: str) -> bytes:
    try:
        return execute_media("preview", phase, data)
    except MediaExecutionError:
        raise DomainError("RENDER_FAILED", "预览渲染失败或超过执行限制", 422) from None


def execute_preview(factory: sessionmaker[Session], settings: Settings, run_id: UUID) -> str:
    store = ObjectStorage(settings)
    with factory.begin() as db:
        if db.get(RenderJob, run_id) is not None:
            raise DomainError("NOT_FOUND", "单图预览任务不存在", 404)
        row = db.scalar(
            select(RenderExecution).where(RenderExecution.id == run_id).with_for_update()
        )
        if row is None:
            raise DomainError("NOT_FOUND", "渲染任务不存在", 404)
        claimed = db.scalar(
            update(RenderExecution)
            .where(RenderExecution.id == run_id, RenderExecution.status == "QUEUED")
            .values(status="RUNNING", error_code=None)
            .returning(RenderExecution.id)
        )
        if claimed is None:
            return row.status
        project_id, actor, input_id, intermediate = (
            row.project_id,
            row.created_by,
            row.input_file_id,
            row.intermediate_key,
        )
    try:
        with factory.begin() as db:
            require_project(db, project_id, actor, write=True)
            row = db.get(RenderExecution, run_id)
            assert row is not None
            if row.storage_identity != identity(store):
                raise DomainError("RENDER_STORAGE_CHANGED", "存储配置已改变", 409)
            source = db.get(StoredFile, input_id)
            assert source is not None
            source_store = ObjectStorage(
                settings.model_copy(
                    update={
                        "object_storage_backend": source.storage_backend.lower(),
                        "s3_bucket": source.bucket or settings.s3_bucket,
                    }
                )
            )
            image = source_store.read(source.object_key, None, source.checksum)
            media_type, name, source_note, rights_note = (
                source.media_type,
                source.original_name,
                source.source_note,
                source.rights_note,
            )
        validate_file(image, "REFERENCE_IMAGE", name, media_type)
        clip = render_phase(image, "encode")
        store.put(intermediate, clip, "video/mp4")
        # Finalization reads the actual intermediate through the private adapter.
        movie = render_phase(
            store.read(intermediate, None, hashlib.sha256(clip).hexdigest()), "finalize"
        )
        file_id = uuid4()
        key = f"rendered/{project_id}/{file_id}"
        with factory.begin() as db:
            require_project(db, project_id, actor, write=True)
            if db.get_bind().dialect.name == "sqlite":
                db.execute(
                    update(Project).where(Project.id == project_id).values(name=Project.name)
                )
                require_project(db, project_id, actor, write=True)
            row = db.get(RenderExecution, run_id)
            assert row is not None
            store.put(key, movie, "video/mp4")
            db.add(
                StoredFile(
                    id=file_id,
                    project_id=project_id,
                    storage_backend=settings.object_storage_backend.upper(),
                    bucket=settings.s3_bucket if store.client is not None else None,
                    object_key=key,
                    checksum=hashlib.sha256(movie).hexdigest(),
                    byte_size=len(movie),
                    media_type="video/mp4",
                    original_name="preview.mp4",
                    source_note=source_note,
                    rights_note=rights_note,
                    rights_confirmed=True,
                    created_by=actor,
                )
            )
            db.flush()
            row.output_file_id, row.status, row.finished_at = file_id, "SUCCEEDED", utcnow()
        return "SUCCEEDED"
    except Exception as exc:
        with factory.begin() as db:
            row = db.get(RenderExecution, run_id)
            assert row is not None
            row.status, row.error_code, row.finished_at = (
                "FAILED",
                exc.code if isinstance(exc, DomainError) else "RENDER_FAILED",
                utcnow(),
            )
        return "FAILED"
