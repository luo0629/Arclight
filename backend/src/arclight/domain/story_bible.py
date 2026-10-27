"""Narrative identities are not visual assets; formal links retain exact asset versions."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arclight.agents.contracts import AssetReference
from arclight.domain.character_names import canonical_name

StoryKind = Literal["WORLD", "CHARACTER", "LOCATION", "PROP", "SCENE"]
Rule = Annotated[str, Field(min_length=1, max_length=500)]
CharacterName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class StoryRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: Literal["governs", "located_in", "appears_in", "uses"]
    target_id: UUID


class StoryEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: StoryKind
    name: str = Field(min_length=1, max_length=100)
    aliases: list[CharacterName] = Field(default_factory=list, max_length=10)
    description: str = Field(default="", max_length=2000)
    rules: list[Rule] = Field(default_factory=list, max_length=10)
    relations: list[StoryRelation] = Field(default_factory=list, max_length=20)
    asset: AssetReference | None = None

    @model_validator(mode="after")
    def unique_links_and_rules(self):
        if self.kind != "CHARACTER" and self.aliases:
            raise ValueError("仅角色支持别名")
        names = [canonical_name(value) for value in [self.name, *self.aliases]]
        if not all(names) or len(set(names)) != len(names):
            raise ValueError("角色名称和别名不能重复或为空")
        links = [(link.relation, link.target_id) for link in self.relations]
        if len(set(links)) != len(links) or len(set(self.rules)) != len(self.rules):
            raise ValueError("规则及关联不能重复")
        if self.kind == "WORLD" and self.asset is not None:
            raise ValueError("世界观不是视觉资产")
        return self
