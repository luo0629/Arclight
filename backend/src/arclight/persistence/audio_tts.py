"""Immutable connection between dialogue production and the existing media queue."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class AudioTTSRequest(Entity, Base):
    __tablename__ = "audio_tts_requests"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("audio_snapshots.id"), index=True)
    line_id: Mapped[str] = mapped_column(String(100))
    voice_version_id: Mapped[UUID] = mapped_column(ForeignKey("audio_voice_versions.id"))
    target_asset_id: Mapped[UUID] = mapped_column(ForeignKey("visual_assets.id"))
    dependency_manifest_id: Mapped[UUID] = mapped_column(ForeignKey("dependency_manifests.id"))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    creation_key: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("project_id", "creation_key", name="uq_audio_tts_creation"),)
