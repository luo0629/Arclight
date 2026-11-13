"""Immutable subtitle production receipts independent of mutable asset state."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class SubtitleProduction(Entity, Base):
    __tablename__ = "subtitle_productions"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    timeline_id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"))
    asset_version_id: Mapped[UUID] = mapped_column(ForeignKey("asset_versions.id"), unique=True)
    dependency_manifest_id: Mapped[UUID] = mapped_column(ForeignKey("dependency_manifests.id"))
    file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id"), unique=True)
    creation_key: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("episode_id", "creation_key"),)
