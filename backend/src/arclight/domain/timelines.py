"""Exact, bounded timeline snapshots shared by storage and later render services."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

TrackKind = Literal["VIDEO", "VOICE", "SFX", "MUSIC", "SUBTITLE"]
TRACK_KINDS: tuple[TrackKind, ...] = ("VIDEO", "VOICE", "SFX", "MUSIC", "SUBTITLE")
Milliseconds = Annotated[int, Field(strict=True, ge=0, le=3_600_000)]


class TimelineContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShotSelection(TimelineContract):
    shot_id: UUID
    asset_version_id: UUID


class TimelineCreate(TimelineContract):
    creation_key: UUID
    storyboard_id: UUID
    default_duration_ms: int = Field(default=3000, strict=True, ge=500, le=30000)
    selected_shots: list[ShotSelection] = Field(min_length=1, max_length=63)

    @model_validator(mode="after")
    def unique_shots(self):
        if len({item.shot_id for item in self.selected_shots}) != len(self.selected_shots):
            raise ValueError("每个镜头只能指定一个精确选用版本")
        return self


class ClipSnapshot(TimelineContract):
    id: UUID
    ordinal: int = Field(strict=True, ge=0, le=255)
    source_asset_version_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_ms: Milliseconds
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    source_start_ms: Milliseconds = 0
    shot_id: UUID | None = None
    script_version_id: UUID | None = None
    line_id: str | None = Field(default=None, min_length=1, max_length=100)
    audio_version_id: UUID | None = None
    review_id: UUID | None = None
    review_storyboard_id: UUID | None = None
    review_shot_id: UUID | None = None

    @model_validator(mode="after")
    def precise_line(self):
        if (self.line_id is None) != (self.script_version_id is None):
            raise ValueError("台词必须同时绑定精确剧本版本和稳定台词ID")
        return self


class TrackSnapshot(TimelineContract):
    id: UUID
    kind: TrackKind
    ordinal: int = Field(strict=True, ge=0, le=4)
    clips: list[ClipSnapshot] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def precise_clips(self):
        if len({item.ordinal for item in self.clips}) != len(self.clips):
            raise ValueError("同一轨道的片段序号不能重复")
        for clip in self.clips:
            if self.kind == "VIDEO" and clip.shot_id is None:
                raise ValueError("画面片段必须关联精确镜头")
            if self.kind in {"VOICE", "SUBTITLE"} and clip.line_id is None:
                raise ValueError("配音和字幕必须关联精确台词")
            if self.kind != "SUBTITLE" and clip.audio_version_id is not None:
                raise ValueError("字幕的音频版本引用只能用于字幕轨")
        return self


class TimelineSnapshot(TimelineContract):
    schema_version: Literal[1] = 1
    episode_id: UUID
    storyboard_id: UUID
    default_duration_ms: int = Field(strict=True, ge=500, le=30000)
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    output_profile: dict[str, JsonValue]
    tracks: list[TrackSnapshot] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def bounded_tracks(self):
        if {track.kind for track in self.tracks} != set(TRACK_KINDS):
            raise ValueError("时间轴必须包含五类独立轨道")
        if {track.ordinal for track in self.tracks} != set(range(5)):
            raise ValueError("轨道序号必须唯一且完整")
        ids = [track.id for track in self.tracks]
        clip_ids = [clip.id for track in self.tracks for clip in track.clips]
        if len(ids) != len(set(ids)) or len(clip_ids) != len(set(clip_ids)):
            raise ValueError("轨道及片段身份不能重复")
        for track in self.tracks:
            if any(clip.start_ms + clip.duration_ms > self.duration_ms for clip in track.clips):
                raise ValueError("片段不能超出时间轴范围")
        video = next(track for track in self.tracks if track.kind == "VIDEO")
        cursor = 0
        for clip in sorted(video.clips, key=lambda item: item.ordinal):
            if clip.start_ms != cursor:
                raise ValueError("默认画面轨必须连续且没有重叠")
            cursor += clip.duration_ms
        if cursor != self.duration_ms:
            raise ValueError("画面轨必须完整覆盖时间轴")
        return self


class TimelineSave(TimelineContract):
    creation_key: UUID
    expected_version_no: int = Field(strict=True, ge=1)
    snapshot: TimelineSnapshot


class TimelineRead(TimelineContract):
    based_on_timeline_id: UUID | None = None
    id: UUID
    asset_id: UUID
    version_no: int
    status: str
    stale: bool
    content_sha256: str
    dependency_manifest_id: UUID
    created_at: datetime
    snapshot: TimelineSnapshot
