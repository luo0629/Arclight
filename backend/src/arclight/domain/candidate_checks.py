"""Deterministic evidence for one immutable image candidate, separate from review."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.asset_versions import MAX_IMAGE_PIXELS


class CheckedImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(pattern=r"^image/(png|jpeg)$")
    byte_size: int = Field(strict=True, gt=0)
    width: int = Field(strict=True, gt=0, le=4096)
    height: int = Field(strict=True, gt=0, le=4096)

    @model_validator(mode="after")
    def bounded_pixels(self):
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image evidence exceeds the STORE pixel limit")
        return self


class CandidateMediaCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["candidate-media-check/1"] = "candidate-media-check/1"
    validator_version: Literal["store-image/1"] = "store-image/1"
    candidate_id: UUID
    input_manifest_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["PASSED", "FAILED"]
    references_valid: bool = Field(strict=True)
    image: CheckedImage | None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.status == "PASSED":
            if self.image is None or not self.references_valid or self.error_code is not None:
                raise ValueError("A passed check needs validated media and exact references")
        elif self.error_code is None:
            raise ValueError("A failed check needs a safe error code")
        return self


class CheckedVideo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["video/mp4"] = "video/mp4"
    byte_size: int = Field(strict=True, gt=0)
    width: int = Field(strict=True, gt=0, le=4096)
    height: int = Field(strict=True, gt=0, le=4096)
    duration_ms: int = Field(strict=True, gt=0, le=30000)
    frame_rate: float = Field(strict=True, gt=0, le=120, allow_inf_nan=False)
    codec: Literal["h264", "hevc", "av1", "mpeg4"]
    decoded_frames: int = Field(strict=True, gt=0, le=3600)
    has_audio: bool = Field(strict=True)
    full_decode_verified: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def explicit_decode_proof(cls, value):
        if isinstance(value, dict) and value.get("full_decode_verified") is not True:
            raise ValueError("Full decoding requires an explicit true boolean proof")
        return value

    @model_validator(mode="after")
    def bounded_video(self):
        if self.width * self.height > 8_294_400 or self.decoded_frames / self.frame_rate > 30:
            raise ValueError("Video evidence exceeds the STORE decoding limits")
        if type(self.full_decode_verified) is not bool:
            raise ValueError("Full decoding requires an explicit boolean proof")
        return self


class CandidateVideoCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["candidate-video-check/1"] = "candidate-video-check/1"
    validator_version: Literal["store-video/1"] = "store-video/1"
    candidate_id: UUID
    input_manifest_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["PASSED", "FAILED"]
    references_valid: bool = Field(strict=True)
    video: CheckedVideo | None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.status == "PASSED":
            if self.video is None or not self.references_valid or self.error_code is not None:
                raise ValueError("A passed check needs decoded video and exact references")
        elif self.error_code is None:
            raise ValueError("A failed check needs a safe error code")
        return self
