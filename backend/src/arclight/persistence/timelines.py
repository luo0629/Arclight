"""Immutable timeline versions with relational exact media and dialogue references."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.domain.timelines import TrackKind
from arclight.persistence.base import JSON_TYPE, Base, Entity


class Timeline(Entity, Base):
    __tablename__ = "timelines"
    id: Mapped[UUID] = mapped_column(ForeignKey("asset_versions.id"), primary_key=True)
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    storyboard_id: Mapped[UUID] = mapped_column(ForeignKey("storyboards.id"))
    version_no: Mapped[int]
    default_duration_ms: Mapped[int]
    duration_ms: Mapped[int]
    output_profile_json: Mapped[dict] = mapped_column(JSON_TYPE)
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    sealed: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        UniqueConstraint("episode_id", "version_no", name="uq_timeline_episode_version"),
        UniqueConstraint("episode_id", "creation_key", name="uq_timeline_episode_creation"),
        CheckConstraint("version_no > 0", name="timeline_version_positive"),
        CheckConstraint(
            "default_duration_ms >= 500 AND default_duration_ms <= 30000",
            name="timeline_default_duration",
        ),
        CheckConstraint("duration_ms > 0 AND duration_ms <= 3600000", name="timeline_duration"),
    )


class TimelineTrack(Entity, Base):
    __tablename__ = "timeline_tracks"
    timeline_id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"), index=True)
    kind: Mapped[TrackKind] = mapped_column(String(16))
    ordinal: Mapped[int]
    __table_args__ = (
        UniqueConstraint("timeline_id", "id", name="uq_timeline_track_identity"),
        UniqueConstraint("timeline_id", "kind", name="uq_timeline_track_kind"),
        UniqueConstraint("timeline_id", "ordinal", name="uq_timeline_track_ordinal"),
        CheckConstraint(
            "kind IN ('VIDEO','VOICE','SFX','MUSIC','SUBTITLE')", name="timeline_track_kind"
        ),
        CheckConstraint("ordinal >= 0 AND ordinal < 5", name="timeline_track_ordinal"),
    )


class TimelineClip(Entity, Base):
    __tablename__ = "timeline_clips"
    timeline_id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"), index=True)
    track_id: Mapped[UUID]
    ordinal: Mapped[int]
    source_asset_version_id: Mapped[UUID] = mapped_column(ForeignKey("asset_versions.id"))
    source_sha256: Mapped[str] = mapped_column(String(64))
    start_ms: Mapped[int]
    duration_ms: Mapped[int]
    source_start_ms: Mapped[int] = mapped_column(default=0)
    shot_id: Mapped[UUID | None] = mapped_column(ForeignKey("shots.id"))
    script_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("scripts.id"))
    line_id: Mapped[str | None] = mapped_column(String(100))
    audio_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("asset_versions.id"))
    review_id: Mapped[UUID | None]
    review_storyboard_id: Mapped[UUID | None] = mapped_column(ForeignKey("storyboards.id"))
    review_shot_id: Mapped[UUID | None] = mapped_column(ForeignKey("shots.id"))
    __table_args__ = (
        ForeignKeyConstraint(
            ["timeline_id", "track_id"], ["timeline_tracks.timeline_id", "timeline_tracks.id"]
        ),
        UniqueConstraint("track_id", "ordinal"),
        CheckConstraint("ordinal >= 0 AND ordinal < 256", name="timeline_clip_ordinal"),
        CheckConstraint(
            "start_ms >= 0 AND duration_ms > 0 AND start_ms + duration_ms <= 3600000 "
            "AND source_start_ms >= 0 AND source_start_ms <= 3600000",
            name="timeline_clip_time",
        ),
        CheckConstraint(
            "(script_version_id IS NULL) = (line_id IS NULL)", name="timeline_clip_line"
        ),
    )


class TimelineRevision(Entity, Base):
    """One immutable save receipt for each compare-and-append operation."""

    __tablename__ = "timeline_revisions"
    id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"), primary_key=True)
    based_on_timeline_id: Mapped[UUID] = mapped_column(ForeignKey("timelines.id"), unique=True)
    expected_version_no: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("expected_version_no > 0", name="timeline_revision_positive"),
    )
