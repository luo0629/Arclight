"""Read exact successful render versions through existing project authorization."""

from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from arclight.application.commands import require_episode
from arclight.application.render_manifests import checked_snapshot
from arclight.core.errors import DomainError
from arclight.domain.render_deliveries import (
    RenderDeliveryDetail,
    RenderDeliveryRead,
    RenderDeliverySource,
)
from arclight.persistence.models import (
    AssetVersion,
    RenderArtifact,
    RenderExecution,
    RenderManifest,
    RenderReportLink,
    StoredFile,
    Timeline,
)


def delivery_query(episode_id: UUID) -> Select[tuple[RenderArtifact]]:
    return (
        select(RenderArtifact)
        .join(RenderManifest, RenderManifest.id == RenderArtifact.manifest_id)
        .join(Timeline, Timeline.id == RenderManifest.timeline_id)
        .join(RenderExecution, RenderExecution.id == RenderArtifact.job_id)
        .where(Timeline.episode_id == episode_id, RenderExecution.status == "SUCCEEDED")
    )


def delivery_row(db: Session, episode_id: UUID, artifact_id: UUID) -> RenderArtifact:
    row = db.scalar(delivery_query(episode_id).where(RenderArtifact.id == artifact_id))
    if row is None:
        raise DomainError("NOT_FOUND", "本剧集的成片版本不存在", 404)
    return row


def delivery_read(db: Session, row: RenderArtifact, episode_id: UUID) -> RenderDeliveryRead:
    version = db.get(AssetVersion, row.asset_version_id)
    manifest = db.get(RenderManifest, row.manifest_id)
    file = db.get(StoredFile, row.file_id)
    assert version is not None and manifest is not None and file is not None
    assert row.job_id is not None
    assert file.storage_backend in ("LOCAL", "S3")
    snapshot = checked_snapshot(manifest)
    report = db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == row.id))
    return RenderDeliveryRead(
        id=row.id,
        project_id=manifest.project_id,
        episode_id=episode_id,
        job_id=row.job_id,
        asset_id=version.asset_id,
        asset_version_id=version.id,
        version_no=version.version_no,
        file_id=file.id,
        sha256=row.checksum,
        byte_size=row.byte_size,
        duration_ms=row.duration_ms,
        profile=snapshot.output.name,
        manifest_id=manifest.id,
        manifest_sha256=manifest.content_sha256,
        timeline_id=manifest.timeline_id,
        dependency_manifest_id=UUID(version.metadata_json["dependency_manifest_id"]),
        inspection_id=UUID(version.metadata_json["inspection_id"]),
        report_id=report.id if report else None,
        report_file_id=report.file_id if report else None,
        storage_backend="LOCAL" if file.storage_backend == "LOCAL" else "S3",
        stale=version.stale,
        created_at=row.created_at,
    )


def list_deliveries(
    db: Session, episode_id: UUID, actor: str, before_id: UUID | None, limit: int
) -> list[RenderDeliveryRead]:
    require_episode(db, episode_id, actor)
    query = delivery_query(episode_id)
    if before_id is not None:
        cursor = delivery_row(db, episode_id, before_id)
        query = query.where(
            or_(
                RenderArtifact.created_at < cursor.created_at,
                and_(RenderArtifact.created_at == cursor.created_at, RenderArtifact.id < cursor.id),
            )
        )
    rows = db.scalars(
        query.order_by(RenderArtifact.created_at.desc(), RenderArtifact.id.desc()).limit(limit)
    )
    return [delivery_read(db, row, episode_id) for row in rows]


def read_delivery(
    db: Session, episode_id: UUID, artifact_id: UUID, actor: str
) -> RenderDeliveryDetail:
    require_episode(db, episode_id, actor)
    row = delivery_row(db, episode_id, artifact_id)
    manifest = db.get(RenderManifest, row.manifest_id)
    assert manifest is not None
    snapshot = checked_snapshot(manifest)
    return RenderDeliveryDetail(
        **delivery_read(db, row, episode_id).model_dump(),
        timeline_sha256=snapshot.timeline_sha256,
        timeline_dependency_manifest_id=snapshot.dependency_manifest_id,
        timeline_dependency_manifest_sha256=snapshot.dependency_manifest_sha256,
        sources=[
            RenderDeliverySource.model_validate(source.model_dump()) for source in snapshot.sources
        ],
        font=snapshot.font,
        software=snapshot.software,
        output=snapshot.output,
    )
