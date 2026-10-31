"""Versioned media capabilities are executable adapter contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationSpec, TaskType


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    width: int = Field(strict=True, ge=1, le=32768)
    height: int = Field(strict=True, ge=1, le=32768)


class MediaCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    task_types: frozenset[TaskType]
    max_reference_images: int = Field(strict=True, ge=0, le=64)
    supports_seed: bool = Field(strict=True)
    max_seed: int = Field(default=9223372036854775807, strict=True, ge=0, le=9223372036854775807)
    min_width: int = Field(default=1, strict=True, ge=1, le=32768)
    min_height: int = Field(default=1, strict=True, ge=1, le=32768)
    max_width: int = Field(strict=True, ge=1, le=32768)
    max_height: int = Field(strict=True, ge=1, le=32768)
    max_pixels: int = Field(strict=True, ge=1, le=268435456)
    dimension_multiple: int = Field(default=1, strict=True, ge=1, le=1024)
    sizes: tuple[Dimensions, ...] = Field(default=(), max_length=100)
    aspect_ratios: tuple[Dimensions, ...] = Field(default=(), max_length=32)
    max_batch_size: int = Field(strict=True, ge=1, le=64)
    supports_query: bool = Field(strict=True)
    supports_cancel: bool = Field(strict=True)
    supports_idempotency: bool = Field(strict=True)
    execution_mode: Literal["SYNC", "ASYNC", "BOTH"]
    max_video_seconds: float | None = Field(default=None, gt=0, le=3600)

    @model_validator(mode="after")
    def coherent(self):
        if (
            not self.task_types
            or self.min_width > self.max_width
            or self.min_height > self.max_height
        ):
            raise ValueError("Capability dimensions and task types must be coherent")
        try:
            for size in self.sizes:
                self.validate_size(size.width, size.height)
        except DomainError as exc:
            raise ValueError("Declared sizes conflict with capability bounds") from exc
        return self

    def validate_size(self, width: int, height: int) -> None:
        if not (
            self.min_width <= width <= self.max_width
            and self.min_height <= height <= self.max_height
        ):
            raise DomainError(
                "PROVIDER_SIZE_UNSUPPORTED", "Output dimensions exceed provider limits", 422
            )
        if (
            width * height > self.max_pixels
            or width % self.dimension_multiple
            or height % self.dimension_multiple
        ):
            raise DomainError(
                "PROVIDER_SIZE_UNSUPPORTED",
                "Output pixels or dimension multiple is unsupported",
                422,
            )
        if self.sizes and not any(
            (size.width, size.height) == (width, height) for size in self.sizes
        ):
            raise DomainError(
                "PROVIDER_SIZE_UNSUPPORTED", "Provider requires a declared output size", 422
            )
        if self.aspect_ratios and not any(
            width * ratio.height == height * ratio.width for ratio in self.aspect_ratios
        ):
            raise DomainError(
                "PROVIDER_ASPECT_UNSUPPORTED", "Output aspect ratio is unsupported", 422
            )


def validate_capabilities(
    capabilities: MediaCapabilities, spec: GenerationSpec, task_type: str, reference_count: int
) -> None:
    if capabilities.provider != spec.provider or task_type not in capabilities.task_types:
        raise DomainError("PROVIDER_TASK_UNSUPPORTED", "Provider does not support this task", 422)
    if reference_count > capabilities.max_reference_images:
        raise DomainError(
            "PROVIDER_REFERENCES_UNSUPPORTED", "Too many reference images for provider", 422
        )
    if spec.seed is not None and (
        not capabilities.supports_seed or spec.seed > capabilities.max_seed
    ):
        raise DomainError("PROVIDER_SEED_UNSUPPORTED", "Provider does not accept a seed", 422)
    if spec.batch_size > capabilities.max_batch_size:
        raise DomainError("PROVIDER_BATCH_UNSUPPORTED", "Batch exceeds provider capability", 422)
    if spec.require_query and not capabilities.supports_query:
        raise DomainError(
            "PROVIDER_QUERY_UNSUPPORTED", "Provider does not support execution queries", 422
        )
    if spec.require_cancel and not capabilities.supports_cancel:
        raise DomainError(
            "PROVIDER_CANCEL_UNSUPPORTED", "Provider does not support cancellation", 422
        )
    output = spec.output_spec
    width, height = output.get("width"), output.get("height")
    if task_type in {"IMAGE", "VIDEO"} or width is not None or height is not None:
        if type(width) is not int or type(height) is not int:
            raise DomainError(
                "INVALID_OUTPUT_SIZE", "Output width and height must be integers", 422
            )
        capabilities.validate_size(width, height)
    duration = output.get("duration_seconds")
    if (
        duration is not None
        and task_type == "VIDEO"
        and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or capabilities.max_video_seconds is None
            or not 0 < duration <= capabilities.max_video_seconds
        )
    ):
        raise DomainError("PROVIDER_DURATION_UNSUPPORTED", "Video duration is unsupported", 422)
