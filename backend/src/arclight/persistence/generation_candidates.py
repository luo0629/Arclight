"""Immutable image import outcomes; candidates never select an asset version."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class GenerationImageImport(Entity, Base):
    __tablename__ = "generation_image_imports"
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"), unique=True)
    output_count: Mapped[int]
    __table_args__ = (CheckConstraint("output_count BETWEEN 1 AND 64", name="image_import_count"),)


class GenerationCandidate(Entity, Base):
    __tablename__ = "generation_candidates"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"))
    input_manifest_id: Mapped[UUID] = mapped_column(ForeignKey("dependency_manifests.id"))
    ordinal: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(16))
    output_handle: Mapped[str | None] = mapped_column(String(512))
    asset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_versions.id"), unique=True
    )
    source_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("stored_files.id"))
    replaces_candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_candidates.id")
    )
    creation_key: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    media_check_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("attempt_id", "ordinal", name="uq_generation_candidate_output"),
        UniqueConstraint("project_id", "creation_key", name="uq_generation_candidate_creation"),
        CheckConstraint(
            "(source='GENERATED' AND ordinal IS NOT NULL AND ordinal >= 0 "
            "AND output_handle IS NOT NULL "
            "AND replaces_candidate_id IS NULL AND source_file_id IS NULL) OR "
            "(source='REPLACEMENT' AND ordinal IS NULL AND output_handle IS NULL "
            "AND replaces_candidate_id IS NOT NULL AND source_file_id IS NOT NULL)",
            name="generation_candidate_source",
        ),
        CheckConstraint(
            "(asset_version_id IS NOT NULL AND error_code IS NULL) OR "
            "(source='GENERATED' AND asset_version_id IS NULL AND error_code IS NOT NULL)",
            name="generation_candidate_outcome",
        ),
    )
