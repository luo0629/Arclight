"""Immutable preparation and exact audio receipt history."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class AudioSnapshot(Entity, Base):
    __tablename__ = "audio_snapshots"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    script_version_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id"))
    script_sha256: Mapped[str] = mapped_column(String(64))
    creation_key: Mapped[UUID]
    lines_json: Mapped[list] = mapped_column(JSON_TYPE)
    line_count: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("episode_id", "creation_key"),
        CheckConstraint("line_count >= 0", name="audio_line_count"),
    )


class AudioLineBinding(Entity, Base):
    __tablename__ = "audio_line_bindings"
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("audio_snapshots.id"), index=True)
    line_id: Mapped[str] = mapped_column(String(100))
    asset_version_id: Mapped[UUID] = mapped_column(ForeignKey("asset_versions.id"), unique=True)
    dependency_manifest_id: Mapped[UUID] = mapped_column(ForeignKey("dependency_manifests.id"))
    file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id"))
    duration_ms: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (CheckConstraint("duration_ms > 0", name="audio_duration"),)
