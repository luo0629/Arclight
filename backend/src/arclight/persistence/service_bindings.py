"""Private immutable credential provenance, kept outside public input manifests."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class GenerationServiceBinding(Entity, Base):
    __tablename__ = "generation_service_bindings"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    created_by: Mapped[str] = mapped_column(String(100))
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("service_credentials.id"))
    configuration_revision: Mapped[int]
    configuration_json: Mapped[dict] = mapped_column(JSON_TYPE)
    configuration_digest: Mapped[str] = mapped_column(String(64))


class GenerationMediaArtifact(Entity, Base):
    __tablename__ = "generation_media_artifacts"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"))
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"))
    ordinal: Mapped[int]
    file_id: Mapped[UUID | None] = mapped_column(ForeignKey("stored_files.id"), unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    media_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        UniqueConstraint("attempt_id", "ordinal", name="uq_generation_media_output"),
        CheckConstraint("ordinal >= 0", name="media_artifact_ordinal"),
        CheckConstraint(
            "(file_id IS NOT NULL AND error_code IS NULL) OR "
            "(file_id IS NULL AND error_code IS NOT NULL)",
            name="media_artifact_outcome",
        ),
    )
