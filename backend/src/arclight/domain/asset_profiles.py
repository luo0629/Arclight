"""Bounded visual profiles with exact, explicitly chosen image references."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.asset_metadata import AssetFields, ShortText


class ReferenceVersion(AssetFields):
    asset_id: UUID
    asset_version_id: UUID


class SceneProfile(AssetFields):
    layout: str = Field(default="", max_length=4000)
    time_of_day: str = Field(default="", max_length=160)
    lighting: str = Field(default="", max_length=2000)
    palette: str = Field(default="", max_length=1000)
    environment: str = Field(default="", max_length=2000)
    reference: ReferenceVersion | None = None


class PropProfile(AssetFields):
    appearance: str = Field(default="", max_length=2000)
    holder: ReferenceVersion | None = None
    story_state: str = Field(default="", max_length=2000)


class Outfit(AssetFields):
    id: UUID
    name: ShortText
    description: str = Field(default="", max_length=1000)


class CharacterImage(AssetFields):
    id: UUID
    label: ShortText
    kind: Literal["INITIAL", "ANGLE"]
    angle: str = Field(default="", max_length=80)
    source: ReferenceVersion

    @model_validator(mode="after")
    def angle_required(self) -> "CharacterImage":
        if self.kind == "ANGLE" and not self.angle:
            raise ValueError("多角度参考必须填写角度")
        return self


class CharacterProfile(AssetFields):
    fixed_features: list[ShortText] = Field(default_factory=list, max_length=30)
    variable_features: list[ShortText] = Field(default_factory=list, max_length=30)
    outfits: list[Outfit] = Field(default_factory=list, max_length=20)
    default_outfit_id: UUID | None = None
    expressions: list[ShortText] = Field(default_factory=list, max_length=30)
    images: list[CharacterImage] = Field(default_factory=list, max_length=30)
    standard_image_id: UUID | None = None

    @model_validator(mode="after")
    def consistent_choices(self) -> "CharacterProfile":
        for values in (self.fixed_features + self.variable_features, self.expressions):
            if len(values) != len(set(values)):
                raise ValueError("特征或表情不可重复，固定与可变特征不能重叠")
        for items in (self.outfits, self.images):
            if len({item.id for item in items}) != len(items):
                raise ValueError("服装或参考图ID不可重复")
        if len({item.name for item in self.outfits}) != len(self.outfits):
            raise ValueError("服装名称不可重复")
        if self.default_outfit_id is not None and self.default_outfit_id not in {
            i.id for i in self.outfits
        }:
            raise ValueError("默认服装必须来自服装列表")
        if self.standard_image_id is not None and self.standard_image_id not in {
            i.id for i in self.images
        }:
            raise ValueError("标准参考图必须来自参考图列表")
        if sum(i.kind == "INITIAL" for i in self.images) > 1:
            raise ValueError("初始设定图只能指定一张")
        if len({(i.source.asset_id, i.source.asset_version_id) for i in self.images}) != len(
            self.images
        ):
            raise ValueError("同一精确参考图版本不可重复登记")
        return self
