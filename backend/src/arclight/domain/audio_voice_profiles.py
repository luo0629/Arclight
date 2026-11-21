"""Story-time selection of immutable character voice versions."""

from datetime import datetime
from itertools import pairwise
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.audio_voices import AudioVoiceRead
from arclight.domain.media_capabilities import NumericParameterRange
from arclight.domain.story_ledger import LedgerText, LedgerValue, StoryTime, intervals_overlap


class VoiceChange(LedgerValue):
    story_start: StoryTime
    story_end: StoryTime | None = None
    voice_version_id: UUID
    reason: LedgerText

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.story_end is not None and self.story_end <= self.story_start:
            raise ValueError("变声时段结束必须晚于开始")
        return self


class VoiceProfilePlan(LedgerValue):
    ledger_version_id: UUID
    default_voice_version_id: UUID
    changes: list[VoiceChange] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def no_overlap(self) -> Self:
        ordered = sorted(self.changes, key=lambda change: change.story_start)
        for left, right in pairwise(ordered):
            if intervals_overlap(
                left.story_start, left.story_end, right.story_start, right.story_end
            ):
                raise ValueError("同一角色的变声时段不可重叠")
        return self

    def change_at(self, time: int) -> VoiceChange | None:
        return next(
            (
                change
                for change in self.changes
                if change.story_start <= time
                and (change.story_end is None or time < change.story_end)
            ),
            None,
        )

    def voice_at(self, time: int) -> UUID:
        change = self.change_at(time)
        return change.voice_version_id if change else self.default_voice_version_id


class VoiceProfileCreate(VoiceProfilePlan):
    creation_key: UUID
    expected_version_no: int = Field(ge=0, strict=True)


class VoiceProfileRead(LedgerValue):
    schema_version: Literal["audio-voice-profile/1"] = "audio-voice-profile/1"
    id: UUID
    character_asset_id: UUID
    version_no: int
    plan: VoiceProfilePlan
    ledger_approval_id: UUID
    created_by: str
    created_at: datetime
    invalidation_id: UUID | None


class LinePerformance(LedgerValue):
    scene_title: str
    sequence_no: int = Field(ge=1, strict=True)
    emotion: str
    purpose: str = ""
    emotion_start: str = ""
    emotion_end: str = ""
    interactions: list[str] = Field(default_factory=list, max_length=100)

    def instruction(self) -> str:
        beats = [self.purpose, self.emotion_start, self.emotion_end, self.emotion]
        direction = "；".join(item for item in [*beats, *self.interactions] if item)
        return f"场景：{self.scene_title}。第{self.sequence_no}句。{direction}"[:2000]


class VoiceSelection(LedgerValue):
    profile_version_id: UUID
    character_asset_id: UUID
    voice_version_id: UUID
    ledger_version_id: UUID
    ledger_approval_id: UUID
    episode_id: UUID
    scene_id: UUID
    mapping_id: UUID
    segment_no: int = Field(ge=1, strict=True)
    story_time: StoryTime
    change_reason: str | None
    performance: LinePerformance


class LineVoiceProfileRead(LedgerValue):
    character_asset_id: UUID | None = None
    profile: VoiceProfileRead | None = None
    voice: AudioVoiceRead | None = None
    selection: VoiceSelection | None = None
    supported_parameters: list[str] = Field(default_factory=list)
    parameter_ranges: dict[str, NumericParameterRange] = Field(default_factory=dict)
    unavailable_reason: str | None = None
