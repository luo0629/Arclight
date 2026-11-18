"""A small allowlisted byte protocol shared by the planner and sealed renderer."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.render_manifests import RenderOutputProfile, SubtitleStyle
from arclight.domain.timeline_audio import ClipAudio
from arclight.domain.timeline_visuals import ClipVisual

MAX_INPUT_BYTES = None
MAX_OUTPUT_BYTES = None
MAX_FRAMES = 18_000
MAX_PIXEL_FRAMES = 10_000_000_000
RENDER_TIMEOUT = 600
AUDIO_RATE = 48_000
MAX_ACTIVE_AUDIO = 16
MAX_AUDIO_DECODE_SAMPLES = AUDIO_RATE * 3600


class RenderVideo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data: str = Field(min_length=1)
    source_start_ms: int = Field(strict=True, ge=0, lt=30000)
    source_end_ms: int = Field(strict=True, gt=0, le=30000)
    frame_rate: float = Field(strict=True, gt=0, le=120, allow_inf_nan=False)
    keep_audio: bool = Field(strict=True)
    motion_required: bool = Field(strict=True)


class RenderClip(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    video: RenderVideo | None = Field(default=None, exclude_if=lambda value: value is None)
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    visual: ClipVisual
    fade_in_ms: int = Field(strict=True, ge=0, le=2000)
    fade_out_ms: int = Field(strict=True, ge=0, le=2000)

    @model_validator(mode="after")
    def exact_source(self):
        if (self.image is None) == (self.video is None):
            raise ValueError("Exactly one image or video source is required")
        if self.video is not None and (
            self.video.source_start_ms + self.duration_ms != self.video.source_end_ms
        ):
            raise ValueError("Video source trim must match its timeline duration")
        return self

    @property
    def embedded_audio(self) -> RenderAudioRange | None:
        if self.video is None or not self.video.keep_audio:
            return None
        return RenderAudioRange(
            start_ms=self.start_ms,
            duration_ms=self.duration_ms,
            source_start_ms=self.video.source_start_ms,
        )


def first_output_frame(start_ms: int, fps: int) -> int:
    return (start_ms * fps + 999) // 1000


class RenderAudioSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data: str = Field(min_length=1)
    media_type: Literal["audio/wav", "audio/mpeg", "audio/ogg", "audio/flac"]


class RenderAudioRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    duration_ms: int = Field(strict=True, gt=0, le=600_000)
    source_start_ms: int = Field(default=0, strict=True, ge=0, le=600_000)
    audio: ClipAudio | None = None

    @model_validator(mode="after")
    def automation_range(self):
        if self.audio is not None:
            self.audio.validate_duration(self.duration_ms)
        return self


class RenderAudioClip(RenderAudioRange):
    source_index: int = Field(strict=True, ge=0, lt=768)


class RenderCue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=2000)
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)

    @model_validator(mode="after")
    def display_text(self):
        if not self.text.strip() or any(
            unicodedata.category(char).startswith("C") and char not in "\n\t" for char in self.text
        ):
            raise ValueError("Invalid subtitle display text")
        return self


class RenderSubtitles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    font: str = Field(min_length=1)
    font_media_type: Literal["font/ttf", "font/otf"]
    style: SubtitleStyle
    cues: tuple[RenderCue, ...] = Field(min_length=1, max_length=256)


def validate_audio_work(ranges: Sequence[tuple[int, int, int]]) -> None:
    """Bound decoder concurrency and decoded prefixes, independently of file byte size."""
    if sum((source + duration) * 48 for _, duration, source in ranges) > MAX_AUDIO_DECODE_SAMPLES:
        raise ValueError("Render exceeds decoded audio workload")
    active = 0
    events = [(start, 1) for start, _, _ in ranges]
    events += [(start + duration, -1) for start, duration, _ in ranges]
    for _, delta in sorted(events):
        active += delta
        if active > MAX_ACTIVE_AUDIO:
            raise ValueError("Render exceeds active audio decoder limit")


class RenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    output: RenderOutputProfile
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    clips: tuple[RenderClip, ...] = Field(min_length=1, max_length=63)
    audio_sources: tuple[RenderAudioSource, ...] = Field(default=(), max_length=768)
    audio_clips: tuple[RenderAudioClip, ...] = Field(default=(), max_length=768)
    subtitles: RenderSubtitles | None = None

    @property
    def frames(self) -> int:
        return first_output_frame(self.duration_ms, self.output.fps)

    @property
    def has_audio_mix(self) -> bool:
        return bool(self.audio_clips) or any(
            clip.video is not None and clip.video.keep_audio for clip in self.clips
        )

    @model_validator(mode="after")
    def bounded_work(self):
        if self.frames > MAX_FRAMES or (
            self.frames * self.output.width * self.output.height > MAX_PIXEL_FRAMES
        ):
            raise ValueError("Render exceeds bounded frame workload")
        cursor = 0
        for clip in self.clips:
            if clip.start_ms != cursor or clip.fade_in_ms + clip.fade_out_ms > clip.duration_ms:
                raise ValueError("Invalid render clip range")
            cursor += clip.duration_ms
            if clip.video is not None and (
                first_output_frame(cursor, self.output.fps)
                - first_output_frame(clip.start_ms, self.output.fps)
                < (2 if clip.video.motion_required else 1)
            ):
                raise ValueError("Video trim has insufficient output frames")
        if cursor != self.duration_ms:
            raise ValueError("Render clips must cover the entire timeline")
        for sound_clip in self.audio_clips:
            if (
                sound_clip.source_index >= len(self.audio_sources)
                or sound_clip.start_ms + sound_clip.duration_ms > self.duration_ms
                or sound_clip.source_start_ms + sound_clip.duration_ms > 600_000
            ):
                raise ValueError("Invalid audio clip range or source")
        embedded = (clip.embedded_audio for clip in self.clips)
        validate_audio_work(
            [
                (clip.start_ms, clip.duration_ms, clip.source_start_ms)
                for clip in (*self.audio_clips, *embedded)
                if clip is not None
            ]
        )
        end = 0
        for cue in self.subtitles.cues if self.subtitles else ():
            if cue.start_ms < end or cue.start_ms + cue.duration_ms > self.duration_ms:
                raise ValueError("Invalid subtitle order, overlap or range")
            end = cue.start_ms + cue.duration_ms
        return self
