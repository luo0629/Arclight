"""Explicit, immutable character and narrator voice configurations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from arclight.domain.workflow_contracts import Checksum


class AudioVoiceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AudioVoiceCreate(AudioVoiceContract):
    creation_key: UUID
    speaker: str = Field(min_length=1, max_length=100)
    character_asset_id: UUID | None = None
    expected_version_no: int = Field(ge=0, strict=True)
    model_config_version: Checksum
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    output_spec: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("speaker")
    @classmethod
    def nonblank_speaker(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("说话者不能为空白")
        return value


class AudioVoiceOption(AudioVoiceContract):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=240)


class AudioVoiceCatalogRead(AudioVoiceContract):
    model_config = ConfigDict(frozen=True)

    status: Literal["READY", "NOT_CONFIGURED", "UNAVAILABLE", "QUERY_FAILED"]
    supplier: str | None = None
    model: str | None = None
    configuration_version: Checksum | None = None
    options: tuple[AudioVoiceOption, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    parameter_ranges: dict[str, dict[str, float]] = Field(default_factory=dict)
    error_code: str | None = None


class AudioVoiceRead(AudioVoiceContract):
    schema_version: Literal["audio-voice/1"] = "audio-voice/1"
    id: UUID
    project_id: UUID
    speaker: str
    character_asset_id: UUID | None
    version_no: int
    provider: str
    model: str
    model_config_version: str
    parameters: dict[str, JsonValue]
    output_spec: dict[str, JsonValue]
    provider_configuration: dict[str, JsonValue]
    created_by: str
    created_at: datetime
