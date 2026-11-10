"""Frozen render inputs and append-only result references; no client command strings."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, model_validator

from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.timelines import TimelineSnapshot

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RenderProfile = Literal["PREVIEW", "FORMAL"]


class RenderContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SubtitleStyle(RenderContract):
    mode: Literal["BURN_IN"] = "BURN_IN"
    font_size: int = Field(default=48, strict=True, ge=6, le=144)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-F]{6}$")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-F]{6}$")
    outline_width: int = Field(default=2, strict=True, ge=0, le=12)
    margin_bottom: int = Field(default=64, strict=True, ge=0, le=500)
    alignment: Literal["BOTTOM_LEFT", "BOTTOM_CENTER", "BOTTOM_RIGHT"] = "BOTTOM_CENTER"
    bold: StrictBool = False


class RenderManifestCreate(RenderContract):
    creation_key: UUID
    profile: RenderProfile = "PREVIEW"
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    font_file_id: UUID | None = None


class OutputGeometry(RenderContract):
    width: int = Field(strict=True, ge=2, le=4096)
    height: int = Field(strict=True, ge=2, le=4096)
    fps: int = Field(strict=True, ge=1, le=120)

    @model_validator(mode="after")
    def supported_geometry(self):
        if self.width % 2 or self.height % 2 or self.width * self.height > 8_294_400:
            raise ValueError("H264输出需要有界的偶数尺寸")
        return self


class RenderOutputProfile(OutputGeometry):
    name: RenderProfile
    container: Literal["mp4"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    video_encoder: Literal["libx264"] = "libx264"
    video_bitrate: int = Field(strict=True, ge=100_000, le=50_000_000)
    pixel_format: Literal["yuv420p"] = "yuv420p"
    audio_codec: Literal["aac"] = "aac"
    audio_bitrate: int = Field(strict=True, ge=32_000, le=320_000)
    audio_sample_rate: Literal[48000] = 48000
    audio_channels: Literal[2] = 2


class RenderFileReference(RenderContract):
    """The immutable StoredFile receipt owns the private backend, bucket and object key."""

    file_id: UUID
    sha256: Sha256
    byte_size: int = Field(strict=True, gt=0)
    media_type: str
    source_note: str
    rights_note: str
    rights_confirmed: StrictBool


class RenderSource(RenderContract):
    asset_id: UUID
    asset_version_id: UUID
    asset_type: str
    sha256: Sha256
    file: RenderFileReference | None = None


class RenderVisual(RenderContract):
    clip_id: UUID
    settings: ClipVisual


class RenderSoftware(RenderContract):
    renderer_version: str = Field(min_length=1, max_length=160)
    pyav_version: str = Field(min_length=1, max_length=80)
    ffmpeg_libraries: dict[str, str]

    @model_validator(mode="after")
    def actual_libraries(self):
        if not {"libavcodec", "libavformat", "libavutil"} <= self.ffmpeg_libraries.keys():
            raise ValueError("必须记录实际FFmpeg运行库版本")
        return self


class RenderManifestSnapshot(RenderContract):
    schema_version: Literal[1] = 1
    timeline_id: UUID
    timeline_sha256: Sha256
    dependency_manifest_id: UUID
    dependency_manifest_sha256: Sha256
    timeline: TimelineSnapshot
    visuals: tuple[RenderVisual, ...]
    sources: tuple[RenderSource, ...]
    output: RenderOutputProfile
    subtitle_style: SubtitleStyle
    font: RenderFileReference | None
    software: RenderSoftware
    created_at: datetime

    @model_validator(mode="after")
    def licensed_font(self):
        if self.font is not None and not self.font.rights_confirmed:
            raise ValueError("字幕字体必须有明确使用授权")
        return self


class RenderManifestRead(RenderContract):
    id: UUID
    project_id: UUID
    created_by: str
    content_sha256: Sha256
    snapshot: RenderManifestSnapshot


class RenderArtifactRead(RenderContract):
    id: UUID
    manifest_id: UUID
    file_id: UUID
    sha256: Sha256
    byte_size: int
    duration_ms: int
    created_at: datetime


class RenderReportEnvelope(RenderContract):
    """RENDER-11 supplies the checks; this envelope binds them to exact inputs/output."""

    schema_version: Literal[1] = 1
    manifest_id: UUID
    manifest_sha256: Sha256
    timeline_id: UUID
    output_file_id: UUID
    output_sha256: Sha256
    details: dict[str, JsonValue]


class RenderReportRead(RenderContract):
    id: UUID
    artifact_id: UUID
    file_id: UUID
    sha256: Sha256
    byte_size: int
    created_at: datetime


class RenderManifestResults(RenderContract):
    manifest_id: UUID
    artifact: RenderArtifactRead | None
    report: RenderReportRead | None
