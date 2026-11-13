"""Versioned dependency requests; the server resolves all exact version snapshots."""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from arclight.core.errors import DomainError

DependencyType = Literal[
    "CONTENT_DEPENDENCY",
    "STYLE_DEPENDENCY",
    "SEQUENCE_DEPENDENCY",
    "REFERENCE_DEPENDENCY",
    "RENDER_DEPENDENCY",
]

IMAGE_KINDS = frozenset(
    {"CHARACTER", "SCENE", "PROP", "STYLE", "SHOT_IMAGE", "IMAGE", "LOCATION", "COSTUME"}
)

# These describe dependency contracts, not availability of media generation APIs.
CONTENT_TARGETS = {
    "SCRIPT": frozenset(
        {
            "SCRIPT",
            "STORYBOARD",
            "SHOT_IMAGE",
            "SHOT_PREVIEW",
            "AUDIO",
            "SUBTITLE",
            "TIMELINE",
            "RENDER",
        }
    ),
    "STORYBOARD": frozenset({"STORYBOARD", "SHOT_IMAGE", "SHOT_PREVIEW", "TIMELINE"}),
    "AUDIO": frozenset({"AUDIO", "SUBTITLE", "TIMELINE"}),
    "SUBTITLE": frozenset({"SUBTITLE", "TIMELINE"}),
    "SHOT_PREVIEW": frozenset({"SHOT_PREVIEW", "TIMELINE"}),
    **{kind: IMAGE_KINDS | {"SHOT_PREVIEW", "TIMELINE"} for kind in IMAGE_KINDS},
}


def validate_dependency_type(source_kind: str, target_kind: str, dependency_type: str) -> None:
    allowed = False
    if dependency_type == "CONTENT_DEPENDENCY":
        allowed = target_kind in CONTENT_TARGETS.get(source_kind, frozenset())
    elif dependency_type == "STYLE_DEPENDENCY":
        allowed = source_kind in IMAGE_KINDS | {"VISUAL_BIBLE"} and target_kind in (
            IMAGE_KINDS | {"SCRIPT", "STORYBOARD", "SHOT_PREVIEW"}
        )
    elif dependency_type == "REFERENCE_DEPENDENCY":
        allowed = source_kind in IMAGE_KINDS and target_kind in IMAGE_KINDS | {"SHOT_PREVIEW"}
    elif dependency_type == "SEQUENCE_DEPENDENCY":
        allowed = (
            source_kind in {"STORYBOARD", "TIMELINE"} and target_kind in {"TIMELINE", "RENDER"}
        ) or (source_kind == "TIMELINE" and target_kind == "SUBTITLE")
    elif dependency_type == "RENDER_DEPENDENCY":
        allowed = source_kind in IMAGE_KINDS | {
            "SHOT_PREVIEW",
            "AUDIO",
            "SUBTITLE",
            "TIMELINE",
        } and target_kind in {"TIMELINE", "RENDER"}
    if not allowed:
        raise DomainError("INVALID_DEPENDENCY_TYPE", "该来源和目标不允许使用此依赖类型", 422)


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
