from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arclight.agents.contracts import Dialogue, Identifier, ShotContent, read_content


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Resource(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    updated_at: datetime


class ProjectCreate(Input):
    name: str = Field(min_length=1, max_length=160)
    style_guide: dict = Field(default_factory=dict)
    output_profile: dict = Field(default_factory=lambda: {"width": 1080, "height": 1920, "fps": 30})
    budget_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class ProjectRead(Resource, ProjectCreate):
    owner_id: str
    archived: bool


class EpisodeCreate(Input):
    episode_no: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=160)
    target_duration_ms: int = Field(default=30000, ge=2500, le=1800000)


class EpisodeRead(Resource, EpisodeCreate):
    project_id: UUID
    status: str


class RunCreate(Input):
    story: str = Field(min_length=1, max_length=20000)


class StoryboardRunCreate(Input):
    script_version_id: UUID


class ReviewCreate(Input):
    resource_type: Literal["script", "storyboard"]
    version_id: UUID
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field(default="", max_length=2000)
    expected_revision: int = Field(ge=0)


class RunRead(Resource):
    episode_id: UUID
    workflow_type: str
    workflow_version: str
    status: str
    trace_id: str
    pending_review: dict | None
    error_code: str | None
    revision: int


class AcceptedRun(BaseModel):
    run_id: UUID
    status: str
    status_url: str
    events_url: str


class ScriptRead(Resource):
    episode_id: UUID
    run_id: UUID | None
    version_no: int
    content_json: dict
    status: str

    @field_validator("content_json")
    @classmethod
    def normalize_content(cls, value: dict) -> dict:
        return read_content(value)


class DialogueLineRead(Dialogue):
    script_version_id: UUID
    scene_id: Identifier
    scene_no: int = Field(gt=0)
    sequence_no: int = Field(gt=0)


class StoryboardRead(ScriptRead):
    script_version_id: UUID


class ShotRead(Resource):
    storyboard_id: UUID
    shot_no: int
    duration_ms: int
    description: str
    content_json: dict
    status: str

    @field_validator("content_json")
    @classmethod
    def normalize_content(cls, value: dict) -> dict:
        return ShotContent.model_validate(value).model_dump(mode="json")


class EventRead(Resource):
    run_id: UUID
    event_type: str
    payload_json: dict
