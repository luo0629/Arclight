"""Shared version contracts for text, art, review and deterministic media services."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from arclight.domain.generation_requests import safe_metadata

Family = Literal["TEXT", "ART", "REVIEW", "MEDIA"]
Kind = Literal["AGENT", "PROMPT", "SCHEMA", "MODEL"]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class VersionRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    family: Family
    kind: Kind
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    definition: dict[str, JsonValue]

    @model_validator(mode="after")
    def safe_definition(self):
        safe_metadata(self.definition)
        if not self.name.strip() or not self.version.strip():
            raise ValueError("版本名称不能为空")
        if len(json.dumps(self.definition, ensure_ascii=False).encode()) > 100000:
            raise ValueError("版本定义过大")
        return self
