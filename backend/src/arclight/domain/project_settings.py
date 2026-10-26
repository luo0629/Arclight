"""Bounded project brief shared by API validation and immutable writer snapshots."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectStorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    description: str = Field(default="", max_length=4000)
    genre: str = Field(default="", max_length=100)
    target_audience: str = Field(default="", max_length=500)
    content_restrictions: str = Field(default="", max_length=4000)
    default_episode_duration_ms: int = Field(default=30000, strict=True, ge=2500, le=1800000)
    target_episode_count: int = Field(default=1, strict=True, ge=1, le=1000)
    episode_plan: str = Field(default="", max_length=4000)
