"""Bounded technical evidence for one exact encoded candidate, before publication."""

from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from arclight.domain.render_manifests import RenderOutputProfile

CheckName = Literal[
    "decoding",
    "tracks",
    "resolution",
    "frame_rate",
    "video_codec",
    "pixel_format",
    "audio_codec",
    "audio_format",
    "duration",
]
InspectionFailure = Literal[
    "INVALID_MEDIA",
    "TIME_LIMIT",
    "MEMORY_LIMIT",
    "RESOURCE_LIMIT",
    "OUTPUT_LIMIT",
    "SANDBOX_UNAVAILABLE",
    "UNAVAILABLE",
    "BUSY",
    "FAILED",
]


class VideoMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    width: int = Field(strict=True, gt=0, le=4096)
    height: int = Field(strict=True, gt=0, le=4096)
    rate_numerator: int = Field(strict=True, gt=0, le=1_000_000)
    rate_denominator: int = Field(strict=True, gt=0, le=1_000_000)
    codec: Literal["h264", "hevc", "av1", "mpeg4", "other"]
    pixel_format: Literal["yuv420p", "other"]
    frames: int = Field(strict=True, ge=0, le=18_000)
    duration_us: int = Field(strict=True, ge=0, le=3_600_000_000)
    declared_duration_us: int = Field(strict=True, ge=0, le=3_600_000_000)
    timing_valid: bool = Field(strict=True)


class AudioMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    codec: Literal["aac", "other"]
    sample_rate: int = Field(strict=True, ge=8000, le=48000)
    channels: int = Field(strict=True, ge=1, le=2)
    samples: int = Field(strict=True, ge=0, le=172_801_024)
    duration_us: int = Field(strict=True, ge=0, le=3_600_022_000)
    declared_duration_us: int = Field(strict=True, ge=0, le=3_600_000_000)
    timing_valid: bool = Field(strict=True)


class RenderMediaMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    streams: tuple[Literal["video", "audio", "subtitle", "data", "attachment", "other"], ...] = (
        Field(max_length=16)
    )
    container_duration_us: int = Field(strict=True, ge=0, le=3_600_000_000)
    decodable: bool = Field(strict=True)
    video: VideoMeasurement | None
    audio: AudioMeasurement | None


class RenderCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: CheckName
    passed: bool = Field(strict=True)


class RenderInspectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    checker: Literal["arclight-ffmpeg-decode-v1"] = "arclight-ffmpeg-decode-v1"
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(strict=True, gt=0)
    measurement: RenderMediaMeasurement | None
    checks: tuple[RenderCheck, ...] = Field(min_length=9, max_length=9)
    failure: InspectionFailure | None = None

    @model_validator(mode="after")
    def unique_checks(self):
        if len({item.name for item in self.checks}) != 9:
            raise ValueError("Each technical check is required exactly once")
        if (self.failure is not None or self.measurement is None) and any(
            c.passed for c in self.checks
        ):
            raise ValueError("An unavailable measurement cannot pass checks")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_code(self) -> str | None:
        if self.failure:
            return f"RENDER_INSPECTION_{self.failure}"
        failed = next((item.name for item in self.checks if not item.passed), None)
        return f"RENDER_OUTPUT_{failed.upper()}" if failed else None


def check_measurement(
    measured: RenderMediaMeasurement | None,
    profile: RenderOutputProfile,
    duration_ms: int,
) -> tuple[RenderCheck, ...]:
    video, audio = (measured.video, measured.audio) if measured else (None, None)
    frames = (duration_ms * profile.fps + 999) // 1000
    duration = Fraction(frames * 1_000_000, profile.fps)
    samples = (frames * profile.audio_sample_rate + profile.fps - 1) // profile.fps
    values: dict[CheckName, bool] = {
        "decoding": bool(
            measured and measured.decodable and video and audio and video.frames and audio.samples
        ),
        "tracks": bool(measured and sorted(measured.streams) == ["audio", "video"]),
        "resolution": bool(
            video and (video.width, video.height) == (profile.width, profile.height)
        ),
        "frame_rate": bool(
            video
            and Fraction(video.rate_numerator, video.rate_denominator) == profile.fps
            and video.timing_valid
        ),
        "video_codec": bool(video and video.codec == profile.video_codec),
        "pixel_format": bool(video and video.pixel_format == profile.pixel_format),
        "audio_codec": bool(audio and audio.codec == profile.audio_codec),
        "audio_format": bool(
            audio
            and audio.sample_rate == profile.audio_sample_rate
            and audio.channels == profile.audio_channels
        ),
        "duration": bool(
            measured
            and video
            and audio
            and video.frames == frames
            and abs(video.duration_us - duration) <= 2
            and abs(video.declared_duration_us - duration) <= 2
            and abs(measured.container_duration_us - duration) <= 1000
            and samples <= audio.samples < samples + 1024
            and abs(audio.duration_us - Fraction(audio.samples * 1_000_000, audio.sample_rate)) <= 2
            and abs(audio.declared_duration_us - duration) <= 1000
            and audio.timing_valid
        ),
    }
    return tuple(RenderCheck(name=name, passed=value) for name, value in values.items())
