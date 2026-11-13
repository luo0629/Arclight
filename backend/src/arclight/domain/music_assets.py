"""Immutable source and inspection contracts for non-dialogue audio assets."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from arclight.domain.audio_production import AudioValidationRead

MusicPurposeRole = Literal["BACKGROUND", "AMBIENCE", "TRANSITION"]


class MusicImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    purpose_role: MusicPurposeRole


class MusicResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["audio-resource/1"] = "audio-resource/1"
    purpose_role: MusicPurposeRole
    source_file_id: UUID
    validation: AudioValidationRead
