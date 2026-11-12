"""Explicit, immutable character and narrator voice configurations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class AudioVoiceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AudioVoiceCreate(AudioVoiceContract):
    creation_key: UUID
    speaker: str = Field(min_length=1, max_length=100)
    character_asset_id: UUID | None = None
    expected_version_no: int = Field(ge=0, strict=True)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    output_spec: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("speaker")
    @classmethod
    def nonblank_speaker(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("说话者不能为空白")
        return value


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
