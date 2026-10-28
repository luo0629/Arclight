"""Versioned dependency requests; the server resolves all exact version snapshots."""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

DependencyType = Literal[
    "CONTENT_DEPENDENCY",
    "STYLE_DEPENDENCY",
    "SEQUENCE_DEPENDENCY",
    "REFERENCE_DEPENDENCY",
    "RENDER_DEPENDENCY",
]


class DependencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_asset_version_id: UUID
    dependency_type: DependencyType
    selectors: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def bounded_selectors(self):
        if len(set(self.selectors)) != len(self.selectors) or any(
            not value or len(value) > 160 or any(ord(c) < 32 for c in value)
            for value in self.selectors
        ):
            raise ValueError("依赖范围必须是唯一、有界的字段或内容身份")
        return self


class ManifestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creation_key: UUID
    target_asset_id: UUID
    inputs: list[DependencyInput] = Field(min_length=1, max_length=64)
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_request(self):
        keys = [(item.source_asset_version_id, item.dependency_type) for item in self.inputs]
        if len(keys) != len(set(keys)):
            raise ValueError("相同版本和类型只能出现一次，请合并 selectors")
        if len(json.dumps(self.context, ensure_ascii=False).encode()) > 65536:
            raise ValueError("输入上下文超出大小限制")
        return self
