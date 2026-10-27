"""Bounded extraction contract; names describe source characters, never asset identities."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arclight.domain.character_names import canonical_name

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ExtractionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )


class ExtractedCharacter(ExtractionModel):
    name: Name
    aliases: list[Name] = Field(max_length=10)
    role: Text
    motivation: Text


class KeyEvent(ExtractionModel):
    title: Name
    description: Text
    character_names: list[Name] = Field(max_length=50)


class StoryAnalysisContent(ExtractionModel):
    schema_version: Literal["1.0"]
    summary: Text
    conflicts: list[Text] = Field(max_length=20)
    key_events: list[KeyEvent] = Field(min_length=1, max_length=50)
    characters: list[ExtractedCharacter] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_names(self) -> "StoryAnalysisContent":
        identities: set[str] = set()
        names = {item.name for item in self.characters}
        for item in self.characters:
            for name in [item.name, *item.aliases]:
                normalized = canonical_name(name)
                if normalized in identities:
                    raise ValueError("角色名或别名重复；请合并同一角色")
                identities.add(normalized)
        for event in self.key_events:
            if len(event.character_names) != len(set(event.character_names)):
                raise ValueError("事件角色引用重复")
            if not set(event.character_names) <= names:
                raise ValueError("事件只能引用角色表中的正式名称")
        return self
