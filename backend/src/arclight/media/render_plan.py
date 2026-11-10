"""A small allowlisted byte protocol shared by the planner and sealed renderer."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.render_manifests import RenderOutputProfile
from arclight.domain.timeline_visuals import ClipVisual

MAX_INPUT_BYTES = 48 * 1024**2
MAX_OUTPUT_BYTES = 128 * 1024**2
MAX_FRAMES = 18_000
MAX_PIXEL_FRAMES = 10_000_000_000
RENDER_TIMEOUT = 600


class RenderClip(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image: str = Field(min_length=1, max_length=1_398_104)
    start_ms: int = Field(strict=True, ge=0, le=3_600_000)
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    visual: ClipVisual
    fade_in_ms: int = Field(strict=True, ge=0, le=2000)
    fade_out_ms: int = Field(strict=True, ge=0, le=2000)


class RenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    output: RenderOutputProfile
    duration_ms: int = Field(strict=True, gt=0, le=3_600_000)
    clips: tuple[RenderClip, ...] = Field(min_length=1, max_length=63)

    @property
    def frames(self) -> int:
        return (self.duration_ms * self.output.fps + 999) // 1000

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
        if cursor != self.duration_ms:
            raise ValueError("Render clips must cover the entire timeline")
        return self
