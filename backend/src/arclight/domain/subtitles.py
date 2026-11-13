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


class SubtitleDisplayCue(SubtitleContract):
    cue_no: int = Field(strict=True, ge=1, le=256)
    text: str = Field(min_length=1, max_length=2000)
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    end_ms: int = Field(strict=True, gt=0, le=3_600_000)

    @field_validator("text")
    @classmethod
    def srt_text(cls, value: str) -> str:
        return SubtitleCue.srt_text(value)

    @model_validator(mode="after")
    def positive_duration(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("字幕结束时间必须晚于开始时间")
        return self


class SubtitleCueEdit(SubtitleContract):
    cue_no: int = Field(strict=True, ge=1, le=256)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    start_ms: int | None = Field(default=None, strict=True, ge=0, le=3_600_000)
    end_ms: int | None = Field(default=None, strict=True, gt=0, le=3_600_000)
    sync_to_audio: bool = Field(default=False, strict=True)

    @field_validator("text")
    @classmethod
    def srt_text(cls, value: str | None) -> str | None:
        return SubtitleCue.srt_text(value) if value is not None else None

    @model_validator(mode="after")
    def complete_change(self):
        if any(
            name in self.model_fields_set and getattr(self, name) is None
            for name in ("text", "start_ms", "end_ms")
        ):
            raise ValueError("字幕修改字段不能为null")
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("字幕起止时间必须同时提供")
        if self.sync_to_audio and self.start_ms is not None:
            raise ValueError("音频同步不能同时指定手动起止时间")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("字幕结束时间必须晚于开始时间")
        if self.text is None and self.start_ms is None and not self.sync_to_audio:
            raise ValueError("至少修改字幕文本、起止时间或执行音频同步")
        return self


class SubtitleEdit(SubtitleContract):
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    changes: list[SubtitleCueEdit] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_cues(self):
        if len({change.cue_no for change in self.changes}) != len(self.changes):
            raise ValueError("同一请求不能重复修改同一句字幕")
        return self


class EditedSubtitleDocument(SubtitleContract):
    schema_version: Literal["subtitle-track/2"] = "subtitle-track/2"
    episode_id: UUID
    timeline_id: UUID
    timeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cues: list[SubtitleCue] = Field(min_length=1, max_length=256)
    display_cues: list[SubtitleDisplayCue] = Field(min_length=1, max_length=256)
    edit_source_version_id: UUID
    edit_source_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    edit_source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_display(self):
        # Keep the generated source contract intact; only the display layer is editable.
        SubtitleDocument(
            episode_id=self.episode_id,
            timeline_id=self.timeline_id,
            timeline_sha256=self.timeline_sha256,
            cues=self.cues,
        )
        if [cue.cue_no for cue in self.display_cues] != [cue.cue_no for cue in self.cues]:
            raise ValueError("显示字幕必须保留原句顺序且逐句对应")
        for previous, current in zip(self.display_cues, self.display_cues[1:], strict=False):
            if current.start_ms < previous.start_ms:
                raise ValueError("字幕时点必须保持原句顺序")
            if current.start_ms < previous.end_ms:
                raise ValueError("字幕显示区间不能重叠")
        return self


SubtitleTrackDocument = SubtitleDocument | EditedSubtitleDocument


def visible_cues(document: SubtitleTrackDocument) -> list[SubtitleCue]:
    if isinstance(document, SubtitleDocument):
        return document.cues
    return [
        source.model_copy(
            update={
                "text": display.text,
                "start_ms": display.start_ms,
                "duration_ms": display.end_ms - display.start_ms,
            }
        )
        for source, display in zip(document.cues, document.display_cues, strict=True)
    ]


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
    schema_version: Literal["subtitle-track/1", "subtitle-track/2"] = "subtitle-track/1"
    source_cues: list[SubtitleCue] | None = None
    edit_source_version_id: UUID | None = None


def srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def srt_bytes(document: SubtitleTrackDocument) -> bytes:
    return "".join(
        f"{cue.cue_no}\n{srt_timestamp(cue.start_ms)} --> "
        f"{srt_timestamp(cue.start_ms + cue.duration_ms)}\n{cue.text}\n\n"
        for cue in visible_cues(document)
    ).encode("utf-8")
