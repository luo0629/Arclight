"""Narrative plans are working data; they cannot publish or approve story assets."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arclight.agents.contracts import Identifier
from arclight.agents.script_rewrite import RewriteTarget
from arclight.agents.story_outline import StoryOutlineContent
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.screenwriter_research import SourceKind

PLAN_VERSION: Literal["screenwriter-plan/1"] = "screenwriter-plan/1"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class PlanningValue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class SeasonConstraints(PlanningValue):
    mode: Literal["season"]
    target_episode_count: int = Field(strict=True, ge=1, le=1000)
    episode_duration_ms: int = Field(strict=True, ge=2500, le=1800000)


class EpisodeConstraints(PlanningValue):
    mode: Literal["episode"]
    target_duration_ms: int = Field(strict=True, ge=2500, le=1800000)
    duration_tolerance_percent: int = Field(strict=True, ge=0, le=100)


class RewriteConstraints(PlanningValue):
    mode: Literal["rewrite"]
    target_duration_ms: int = Field(strict=True, ge=2500, le=1800000)
    duration_tolerance_percent: int = Field(strict=True, ge=0, le=100)
    base_version_id: UUID
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: RewriteTarget


PlanningConstraints = Annotated[
    SeasonConstraints | EpisodeConstraints | RewriteConstraints, Field(discriminator="mode")
]


class PlanningSeed(PlanningValue):
    constraints: PlanningConstraints
    phase: Literal["PLANNING_ONLY"] = "PLANNING_ONLY"


class PlanEvidence(PlanningValue):
    """An exact material from a successful investigation in this planning operation."""

    manifest_id: UUID
    source_kind: Literal["STARTUP"] | SourceKind
    source_id: UUID
    bound_manifest_id: UUID
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class OpenQuestion(PlanningValue):
    question: Text
    reason: Literal["MISSING_EVIDENCE", "CONSTRAINT_CONFLICT", "LOW_CONFIDENCE", "USER_DECISION"]
    blocking: bool = Field(strict=True)


class SceneGoal(PlanningValue):
    scene_id: Identifier
    goal: Text
    beats: list[Text] = Field(min_length=1, max_length=12)


class PlannedScene(SceneGoal):
    title: str = Field(min_length=1, max_length=200)
    estimated_duration_ms: int = Field(strict=True, gt=0, le=1800000)


class SeasonPlan(PlanningValue):
    mode: Literal["season"]
    outline: StoryOutlineContent


class EpisodePlan(PlanningValue):
    mode: Literal["episode"]
    target_duration_ms: int = Field(strict=True, ge=2500, le=1800000)
    estimated_duration_ms: int = Field(strict=True, gt=0, le=1800000)
    scenes: list[PlannedScene] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def complete_scene_timing(self):
        if sum(scene.estimated_duration_ms for scene in self.scenes) != self.estimated_duration_ms:
            raise ValueError("Scene estimates must add up to the episode estimate")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene plan identities must be unique")
        return self


class RewritePlan(PlanningValue):
    mode: Literal["rewrite"]
    base_version_id: UUID
    target: RewriteTarget
    estimated_duration_ms: int = Field(strict=True, gt=0, le=1800000)
    scenes: list[SceneGoal] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_scene_goals(self):
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Rewrite scene goals must be unique")
        return self


class ScreenwriterPlan(PlanningValue):
    schema_version: Literal["screenwriter-plan/1"] = PLAN_VERSION
    objective: Text
    goals: list[Text] = Field(min_length=1, max_length=16)
    plan: Annotated[SeasonPlan | EpisodePlan | RewritePlan, Field(discriminator="mode")]
    evidence: list[PlanEvidence] = Field(min_length=1, max_length=32)
    open_questions: list[OpenQuestion] = Field(max_length=16)

    @model_validator(mode="after")
    def bounded_working_plan(self):
        value = self.model_dump(mode="json")
        safe_metadata(value)
        if len({item.model_dump_json() for item in self.evidence}) != len(self.evidence):
            raise ValueError("Plan citations must be unique")
        if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
            raise ValueError("Plan exceeds its byte limit; request a smaller task")
        return self
