"""Bounded, complete season outline. Each revision keeps its own planning contract."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class OutlineEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    episode_no: int = Field(strict=True, ge=1, le=1000)
    title: str = Field(min_length=1, max_length=160)
    goal: Text
    conflict: Text
    turning_point: Text
    ending_hook: Text


class StoryOutlineContent(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    schema_version: Literal["1.0"]
    target_episode_count: int = Field(strict=True, ge=1, le=1000)
    episode_duration_ms: int = Field(strict=True, ge=2500, le=1800000)
    episodes: list[OutlineEpisode] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def complete_plan(self) -> "StoryOutlineContent":
        if [item.episode_no for item in self.episodes] != list(
            range(1, self.target_episode_count + 1)
        ):
            raise ValueError("分集必须按目标集数从1开始连续编号，不能缺集或重复")
        return self
