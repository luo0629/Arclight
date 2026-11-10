"""Trusted renderer/report writers append receipts without editing the input manifest.

Binding an output records its actual identity, including failed inspection outputs.
It does not approve a delivery or assert that its streams match the requested profile.
"""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.render_inputs import file_reference, file_storage
from arclight.application.render_manifests import manifest_row, read_render_manifest
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import (
    RenderArtifactRead,
    RenderManifestResults,
    RenderReportEnvelope,
    RenderReportRead,
)
from arclight.media.versions import renderer_software
from arclight.persistence.models import StoredFile
from arclight.persistence.render_manifests import RenderArtifact, RenderReportLink

MAX_RENDER_BYTES = 512 * 1024**2
MAX_REPORT_BYTES = 256 * 1024


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def artifact_read(row: RenderArtifact) -> RenderArtifactRead:
    return RenderArtifactRead(
        id=row.id,
        job_id=row.job_id,
        asset_version_id=row.asset_version_id,
        manifest_id=row.manifest_id,
        file_id=row.file_id,
        sha256=row.checksum,
        byte_size=row.byte_size,
        duration_ms=row.duration_ms,
        created_at=utc(row.created_at),
    )


def report_read(row: RenderReportLink) -> RenderReportRead:
    return RenderReportRead(
        id=row.id,
        artifact_id=row.artifact_id,
        file_id=row.file_id,
        sha256=row.checksum,
        byte_size=row.byte_size,
        created_at=utc(row.created_at),
    )


def result_file(db: Session, project_id: UUID, file_id: UUID, media_type: str) -> StoredFile:
    row = db.get(StoredFile, file_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "本项目的渲染结果文件不存在", 404)
    if row.media_type != media_type:
        raise DomainError("RENDER_RESULT_INVALID", "渲染结果文件类型不符", 409)
    file_reference(row)
    return row


def bind_render_artifact(
    db: Session,
    settings: Settings,
    manifest_id: UUID,
    file_id: UUID,
    duration_ms: int,
    actor: str,
) -> RenderArtifactRead:
    """The controlled encoder supplies measured duration; RENDER-09 owns stream acceptance."""
    row = manifest_row(db, manifest_id, actor, write=True)
    manifest = read_render_manifest(db, manifest_id, actor)
    if type(duration_ms) is not int or not 0 < duration_ms <= 3_600_000:
        raise DomainError("RENDER_RESULT_INVALID", "产物实测时长无效", 422)
    previous = db.scalar(
        select(RenderArtifact)
        .where(RenderArtifact.manifest_id == manifest_id, RenderArtifact.job_id.is_(None))
        .order_by(RenderArtifact.created_at.desc(), RenderArtifact.id.desc())
        .limit(1)
    )
    if previous:
        if previous.file_id != file_id or previous.duration_ms != duration_ms:
            raise DomainError("RENDER_RESULT_CONFLICT", "渲染产物回执不能替换", 409)
        return artifact_read(previous)
    if renderer_software() != manifest.snapshot.software:
        raise DomainError("RENDER_SOFTWARE_CHANGED", "渲染软件已变更，须创建新清单", 409)
    if db.scalar(select(RenderArtifact.id).where(RenderArtifact.file_id == file_id)):
        raise DomainError("RENDER_RESULT_CONFLICT", "产物已绑定其他清单，不能借用历史回执", 409)
    file = result_file(db, row.project_id, file_id, "video/mp4")
    file_storage(settings, file).verify(
        file.object_key, file.byte_size, file.checksum, MAX_RENDER_BYTES
    )
    receipt = RenderArtifact(
        manifest_id=manifest_id,
        file_id=file.id,
        object_key=file.object_key,
        checksum=file.checksum,
        byte_size=file.byte_size,
        duration_ms=duration_ms,
    )
    db.add(receipt)
    db.flush()
    audit(
        db, row.project_id, actor, "render.artifact_bound", receipt.id, {"manifest_id": str(row.id)}
    )
    return artifact_read(receipt)


def bind_render_report(
    db: Session, settings: Settings, manifest_id: UUID, file_id: UUID, actor: str
) -> RenderReportRead:
    row = manifest_row(db, manifest_id, actor, write=True)
    read_render_manifest(db, manifest_id, actor)
    if not db.scalar(
        select(RenderArtifact.id).where(RenderArtifact.manifest_id == manifest_id).limit(1)
    ):
        raise DomainError("RENDER_ARTIFACT_REQUIRED", "报告必须关联已登记的精确产物", 409)
    borrowed = db.scalar(
        select(RenderReportLink.id)
        .join(RenderArtifact, RenderArtifact.id == RenderReportLink.artifact_id)
        .where(
            RenderReportLink.file_id == file_id,
            RenderArtifact.manifest_id != manifest_id,
        )
    )
    if borrowed:
        raise DomainError(
            "RENDER_RESULT_CONFLICT", "报告已绑定其他产物，不能替换或借用历史回执", 409
        )
    file = result_file(db, row.project_id, file_id, "application/json")
    data = file_storage(settings, file).read(file.object_key, MAX_REPORT_BYTES, file.checksum)
    try:
        envelope = RenderReportEnvelope.model_validate_json(data)
    except ValidationError:
        raise DomainError("RENDER_REPORT_INVALID", "报告不符合精确关联格式", 409) from None
    artifact = db.scalar(
        select(RenderArtifact).where(
            RenderArtifact.manifest_id == manifest_id,
            RenderArtifact.file_id == envelope.output_file_id,
            RenderArtifact.checksum == envelope.output_sha256,
        )
    )
    if (
        artifact is None
        or len(data) != file.byte_size
        or (
            envelope.manifest_id,
            envelope.manifest_sha256,
            envelope.timeline_id,
        )
        != (row.id, row.content_sha256, row.timeline_id)
    ):
        raise DomainError("RENDER_REPORT_INVALID", "报告与精确清单或产物不匹配", 409)
    previous = db.scalar(
        select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id)
    )
    if previous:
        if previous.file_id != file_id:
            raise DomainError("RENDER_RESULT_CONFLICT", "验收报告回执不能替换", 409)
        return report_read(previous)
    if db.scalar(select(RenderReportLink.id).where(RenderReportLink.file_id == file_id)):
        raise DomainError("RENDER_RESULT_CONFLICT", "报告已绑定其他产物，不能借用历史回执", 409)
    receipt = RenderReportLink(
        artifact_id=artifact.id,
        file_id=file.id,
        object_key=file.object_key,
        checksum=file.checksum,
        byte_size=file.byte_size,
    )
    db.add(receipt)
    db.flush()
    audit(
        db, row.project_id, actor, "render.report_bound", receipt.id, {"manifest_id": str(row.id)}
    )
    return report_read(receipt)


def read_render_results(db: Session, manifest_id: UUID, actor: str) -> RenderManifestResults:
    read_render_manifest(db, manifest_id, actor)
    artifact = db.scalar(
        select(RenderArtifact)
        .where(RenderArtifact.manifest_id == manifest_id)
        .order_by(RenderArtifact.created_at.desc(), RenderArtifact.id.desc())
        .limit(1)
    )
    report = (
        db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id))
        if artifact
        else None
    )
    return RenderManifestResults(
        manifest_id=manifest_id,
        artifact=artifact_read(artifact) if artifact else None,
        report=report_read(report) if report else None,
    )
