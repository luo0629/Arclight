"""Explicit dialogue TTS requests and immutable result receipts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from arclight.domain.audio_production import AudioBindingRead, AudioContract, AudioValidationRead


class AudioTTSCreate(AudioContract):
    creation_key: UUID
    voice_version_id: UUID
    target_asset_id: UUID | None = None
    expected_revision: int = Field(default=0, strict=True, ge=0)
    performance_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    batch_id: UUID | None = None
    scheduling_mode: Literal["INTERACTIVE", "BATCH"] = "INTERACTIVE"


class AudioTTSRead(AudioContract):
    job_id: UUID
    snapshot_id: UUID
    line_id: str
    voice_version_id: UUID
    target_asset_id: UUID
    dependency_manifest_id: UUID
    generation_manifest_id: UUID
    status: str
    revision: int
    error_code: str | None
    asset_version_id: UUID | None
    created_at: datetime


class AudioTTSRegister(AudioContract):
    expected_revision: int = Field(strict=True, ge=0)
    source_note: str = Field(min_length=1, max_length=1000)
    rights_note: str = Field(min_length=1, max_length=1000)
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认生成音频的使用权")
        return value

    @field_validator("source_note", "rights_note")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("来源和授权说明不能为空白")
        return value


class AudioTTSResultRead(AudioContract):
    job_id: UUID
    attempt_id: UUID
    source_file_id: UUID
    voice_version_id: UUID
    binding: AudioBindingRead
    validation: AudioValidationRead
    asset_revision: int
