"""Voice defaults are append-only versions, never mutable account configuration."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class AudioVoiceVersion(Entity, Base):
    __tablename__ = "audio_voice_versions"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    speaker: Mapped[str] = mapped_column(String(100))
    character_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("visual_assets.id"))
    version_no: Mapped[int]
    creation_key: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    configuration_json: Mapped[dict] = mapped_column(JSON_TYPE)
    service_snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint(
            "project_id", "speaker", "version_no", name="uq_audio_voice_speaker_version"
        ),
        UniqueConstraint("project_id", "creation_key", name="uq_audio_voice_creation"),
        CheckConstraint("version_no > 0", name="audio_voice_number"),
    )
