"""Frozen inputs for the bounded local mouth-pose correction route."""

from itertools import pairwise
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_SYNC_MS = 15_000
MAX_SYNC_LINES = 12
SYNC_ENGINE: Literal["local-mouth-pose/1"] = "local-mouth-pose/1"
Time = Annotated[int, Field(strict=True, ge=0, lt=MAX_SYNC_MS)]
Checksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SyncContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MouthRegion(SyncContract):
    """A mouth-sized region in basis points of the decoded, unscaled frame."""

    x: int = Field(strict=True, ge=0, lt=10000)
    y: int = Field(strict=True, ge=0, lt=10000)
    width: int = Field(strict=True, gt=0, le=4000)
    height: int = Field(strict=True, gt=0, le=3000)

    @model_validator(mode="after")
    def inside_frame(self) -> Self:
        if self.x + self.width > 10000 or self.y + self.height > 10000:
            raise ValueError("口部区域必须位于完整视频画面内")
        return self


class LipSyncCue(SyncContract):
    line_id: str = Field(min_length=1, max_length=120)
    audio_version_id: UUID
    start_ms: Time
    closed_frame_ms: Time
    open_frame_ms: Time
    mouth: MouthRegion

    @model_validator(mode="after")
    def distinct_poses(self) -> Self:
        if self.closed_frame_ms == self.open_frame_ms:
            raise ValueError("闭嘴与张嘴参考须来自不同帧")
        return self


class LipSyncCreate(SyncContract):
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    cues: tuple[LipSyncCue, ...] = Field(min_length=1, max_length=MAX_SYNC_LINES)
    source_rights_note: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
        | None
    ) = None


class LipSyncAudioOption(SyncContract):
    audio_version_id: UUID
    duration_ms: int
    name: str


class LipSyncDialogueOption(SyncContract):
    line_id: str
    speaker: str
    text: str
    presentation: Literal["visible", "voice_over", "off_screen", "obscured"]
    lipsync: Literal["required", "not_applicable"]
    exemption_reason: str
    sources: list[LipSyncAudioOption]


class LipSyncOptions(SyncContract):
    candidate_id: UUID
    requires_video_rights_confirmation: bool
    dialogue: list[LipSyncDialogueOption]


class LipSyncLine(LipSyncCue):
    audio_file_id: UUID
    audio_sha256: Checksum
    duration_ms: int = Field(strict=True, gt=0, le=MAX_SYNC_MS)

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


class LipSyncReceipt(SyncContract):
    engine: Literal["local-mouth-pose/1"] = SYNC_ENGINE
    source_candidate_id: UUID
    source_video_version_id: UUID
    source_video_sha256: Checksum
    script_version_id: UUID
    output_sha256: Checksum
    lines: tuple[LipSyncLine, ...] = Field(min_length=1, max_length=MAX_SYNC_LINES)

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if (
            len({line.line_id for line in self.lines}) != len(self.lines)
            or any(right.start_ms < left.end_ms for left, right in pairwise(self.lines))
            or self.lines[-1].end_ms > MAX_SYNC_MS
        ):
            raise ValueError("口型回执的精确台词须唯一、有序且不重叠")
        return self
