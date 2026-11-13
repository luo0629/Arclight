"""Frozen subtitle production and canonical UTF-8 SRT contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arclight.domain.timelines import ClipSnapshot


class SubtitleContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class LegacySubtitleAssetMetadata(SubtitleContract):
    episode_id: UUID | None = None


class SubtitleAssetMetadata(SubtitleContract):
    episode_id: UUID
    subtitle_production: Literal[1]


class SubtitleGenerate(SubtitleContract):
    creation_key: UUID
    timeline_id: UUID
    target_asset_id: UUID | None = None
    expected_revision: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def target_revision(self):
        if (self.target_asset_id is None) != (self.expected_revision is None):
            raise ValueError("已有字幕资产必须提供目录版本，新建资产不得提供目录版本")
        return self


class SubtitleCue(SubtitleContract):
    cue_no: int = Field(strict=True, ge=1, le=256)
    text: str = Field(min_length=1, max_length=2000)
    speaker: str = Field(min_length=1, max_length=100)
    line_type: Literal["dialogue", "narration"]
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    source_start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    source_clip_id: UUID
    script_version_id: UUID
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_id: str = Field(min_length=1, max_length=100)
    audio_asset_version_id: UUID
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audio_snapshot_id: UUID
    audio_binding_id: UUID
    audio_file_id: UUID

    @field_validator("text")
    @classmethod
    def srt_text(cls, value: str) -> str:
        # Blank lines and carriage returns would create ambiguous SRT cue boundaries.
        if (
            not value.strip()
            or "\r" in value
            or "\n\n" in value
            or any(ord(char) < 32 and char not in "\n\t" for char in value)
        ):
            raise ValueError("字幕文本必须是非空的规范文本，不能包含空段或控制字符")
        return value

    @model_validator(mode="after")
    def bounded_end(self):
        if self.start_ms + self.duration_ms > 3_600_000:
            raise ValueError("字幕结束时间超出时间轴范围")
        return self


class SubtitleDocument(SubtitleContract):
    schema_version: Literal["subtitle-track/1"] = "subtitle-track/1"
    episode_id: UUID
    timeline_id: UUID
    timeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cues: list[SubtitleCue] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def ordered_cues(self):
        if [cue.cue_no for cue in self.cues] != list(range(1, len(self.cues) + 1)):
            raise ValueError("字幕序号必须连续")
        if self.cues != sorted(self.cues, key=lambda cue: (cue.start_ms, str(cue.source_clip_id))):
            raise ValueError("字幕必须按精确时间和来源片段排序")
        if len({cue.source_clip_id for cue in self.cues}) != len(self.cues):
            raise ValueError("同一来源片段只能生成一个字幕")
        return self


class SubtitleTrackRead(SubtitleContract):
    id: UUID
    asset_id: UUID
    version_no: int
    status: str
    stale: bool
    checksum: str
    dependency_manifest_id: UUID
    file_id: UUID
    timeline_id: UUID
    created_at: datetime
    cues: list[SubtitleCue]
    clips: list[ClipSnapshot]


def srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def srt_bytes(document: SubtitleDocument) -> bytes:
    return "".join(
        f"{cue.cue_no}\n{srt_timestamp(cue.start_ms)} --> "
        f"{srt_timestamp(cue.start_ms + cue.duration_ms)}\n{cue.text}\n\n"
        for cue in document.cues
    ).encode("utf-8")
