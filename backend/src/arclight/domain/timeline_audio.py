"""Bounded, immutable clip-local gain automation; mixing belongs to the renderer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AudioEnvelopePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offset_ms: int = Field(strict=True, ge=0, le=3_600_000)
    gain_milli: int = Field(strict=True, ge=0, le=4000)


class ClipAudio(BaseModel):
    """1000 is unity; interpolate relative envelope gains and multiply linear fades."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose_role: Literal["BACKGROUND", "AMBIENCE", "TRANSITION"]
    gain_milli: int = Field(default=1000, strict=True, ge=0, le=4000)
    fade_in_ms: int = Field(default=0, strict=True, ge=0, le=3_600_000)
    fade_out_ms: int = Field(default=0, strict=True, ge=0, le=3_600_000)
    envelope: tuple[AudioEnvelopePoint, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def ordered_envelope(self):
        if self.envelope and (
            len(self.envelope) < 2
            or self.envelope[0].offset_ms != 0
            or any(
                left.offset_ms >= right.offset_ms
                for left, right in zip(self.envelope, self.envelope[1:], strict=False)
            )
        ):
            raise ValueError("音量包络须由零开始，至少两点且时间严格递增")
        return self

    def validate_duration(self, duration_ms: int) -> None:
        if self.fade_in_ms + self.fade_out_ms > duration_ms:
            raise ValueError("音频淡入淡出窗口不能越过片段时长或互相重叠")
        if self.envelope and self.envelope[-1].offset_ms != duration_ms:
            raise ValueError("音量包络末点须为片段结束时间")
