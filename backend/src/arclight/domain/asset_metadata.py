"""Bounded asset metadata; declared scope is separate from actual usage."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class AssetFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Appearance(AssetFields):
    age_range: str = Field(default="", max_length=160)
    appearance: str = Field(default="", max_length=2000)
    hairstyle: str = Field(default="", max_length=500)
    clothing: str = Field(default="", max_length=1000)
    build: str = Field(default="", max_length=500)
    expressions: str = Field(default="", max_length=500)
    colors: str = Field(default="", max_length=500)
    materials: str = Field(default="", max_length=500)


class AssetScope(AssetFields):
    kind: Literal["PROJECT", "EPISODES"] = "PROJECT"
    episode_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_scope(self) -> "AssetScope":
        if self.kind == "PROJECT" and self.episode_ids:
            raise ValueError("项目范围不应包含剧集 ID")
        if self.kind == "EPISODES" and not self.episode_ids:
            raise ValueError("剧集范围至少选择一集")
        if len(set(self.episode_ids)) != len(self.episode_ids):
            raise ValueError("剧集 ID 不可重复")
        return self


class AssetOrigin(AssetFields):
    kind: Literal["MANUAL", "IMPORTED"] = "MANUAL"
    note: str = Field(default="", max_length=2000)


class AssetMetadata(AssetFields):
    description: str = Field(default="", max_length=4000)
    tags: list[ShortText] = Field(default_factory=list, max_length=20)
    appearance: Appearance = Field(default_factory=Appearance)
    usage_scope: AssetScope = Field(default_factory=AssetScope)

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, tags: list[str]) -> list[str]:
        if len(set(tags)) != len(tags):
            raise ValueError("标签不可重复")
        return tags
