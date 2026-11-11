"""Immutable render inputs, output receipts and exact machine-report associations."""

from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class RenderManifest(Entity, Base):
    __tablename__ = "render_manifests"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    timeline_id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"), index=True)
    dependency_manifest_id: Mapped[UUID] = mapped_column(ForeignKey("dependency_manifests.id"))
    font_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("stored_files.id"))
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key", name="uq_render_manifest_creation"),
    )


class RenderArtifact(Entity, Base):
    __tablename__ = "render_artifacts"
    manifest_id: Mapped[UUID] = mapped_column(ForeignKey("render_manifests.id"), index=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("render_jobs.id"), unique=True)
    asset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_versions.id"), unique=True
    )
    file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id"), unique=True)
    object_key: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int]
    duration_ms: Mapped[int]
    __table_args__ = (
        Index(
            "uq_render_artifact_manual_manifest",
            "manifest_id",
            unique=True,
            sqlite_where=text("job_id IS NULL"),
            postgresql_where=text("job_id IS NULL"),
        ),
        CheckConstraint(
            "(job_id IS NULL AND asset_version_id IS NULL) OR "
            "(job_id IS NOT NULL AND asset_version_id IS NOT NULL)",
            name="render_artifact_registration",
        ),
        CheckConstraint("byte_size > 0", name="render_artifact_size"),
        CheckConstraint(
            "duration_ms > 0 AND duration_ms <= 3600000", name="render_artifact_duration"
        ),
    )


class RenderReportLink(Entity, Base):
    __tablename__ = "render_report_links"
    artifact_id: Mapped[UUID] = mapped_column(ForeignKey("render_artifacts.id"), unique=True)
    file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id"), unique=True)
    object_key: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int]
    inspection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("render_inspections.id"), unique=True
    )
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    machine_report_json: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="render_report_size"),
        CheckConstraint(
            "(inspection_id IS NULL AND input_sha256 IS NULL AND machine_report_json IS NULL) OR "
            "(inspection_id IS NOT NULL AND input_sha256 IS NOT NULL "
            "AND machine_report_json IS NOT NULL)",
            name="render_report_machine_provenance",
        ),
    )
