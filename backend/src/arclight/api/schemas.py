from datetime import datetime
from decimal import Decimal
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

from arclight.agents.contracts import Dialogue, Identifier, read_content, read_shot_content
from arclight.domain.director_revision import DirectorRevisionOptions
from arclight.domain.project_settings import ProjectStorySettings
from arclight.domain.screenwriter_revision import ScreenwriterRevisionOptions
from arclight.domain.script_drafts import ScriptWritingPurpose
from arclight.domain.script_narrative import NarrativeRequest
from arclight.domain.visual_bible import VisualPatch
from arclight.domain.workflow_contracts import WorkflowRunContract


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Resource(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    updated_at: datetime


class ProjectCreate(Input):
    story_settings: ProjectStorySettings = Field(default_factory=ProjectStorySettings)
    name: str = Field(min_length=1, max_length=160)
    style_guide: dict = Field(default_factory=dict)
    output_profile: dict = Field(default_factory=lambda: {"width": 1080, "height": 1920, "fps": 30})
    budget_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class VisualBibleEdit(Input):
    expected_version_no: int = Field(strict=True, ge=0)
    creation_key: UUID
    fields: VisualPatch


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


class RunCreate(Input, ScreenwriterRevisionOptions, DirectorRevisionOptions):
    purpose: ScriptWritingPurpose = "REVIEW_WORKFLOW"
    professional_director: bool = Field(default=False, strict=True)
    story: Annotated[str, StringConstraints(strip_whitespace=False)] | None = Field(
        default=None, min_length=1, max_length=20000
    )
    outline_version_id: UUID | None = None
    source_id: UUID | None = None
    narrative: NarrativeRequest | None = None

    @model_validator(mode="after")
    def one_source(self) -> "RunCreate":
        if self.purpose == "EDITABLE_DRAFT" and self.professional_director:
            raise ValueError("剧本草稿不包含分镜任务")
        if self.narrative is not None and (not self.professional or self.professional_director):
            raise ValueError("连续剧情创作须选择独立专业编剧")
        if self.professional_director and not self.professional:
            raise ValueError("专业双审核须选择专业编剧；独立导演使用storyboard-runs入口")
        if self.director_art_collaboration and not self.professional_director:
            raise ValueError("美术协作须选择专业导演")
        if self.director_revision_rounds is not None and not self.professional_director:
            raise ValueError("导演修订须选择专业导演")
        if (
            sum(
                value is not None for value in (self.story, self.outline_version_id, self.source_id)
            )
            != 1
        ):
            raise ValueError("请提供故事文本、精确来源或已确认的精确大纲版本，三选一")
        return self


class StoryboardRunCreate(Input, DirectorRevisionOptions):
    script_version_id: UUID
    rebuild_from_version_id: UUID | None = None
    performance: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def performance_director(self):
        if self.performance and (
            not self.professional
            or self.director_revision_rounds is None
            or self.director_art_collaboration
        ):
            raise ValueError("表演分镜须使用独立专业导演修订流程")
        return self


class ReviewCreate(Input):
    performance_intent_only: bool = Field(
        default=False, strict=True, exclude_if=lambda value: not value
    )
    resource_type: Literal["script", "storyboard"]
    version_id: UUID
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field(default="", max_length=2000)
    expected_revision: int = Field(ge=0)
    impact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RunRead(Resource):
    episode_id: UUID
    workflow_type: str
    workflow_version: str
    status: str
    trace_id: str
    pending_review: dict | None
    error_code: str | None
    revision: int
    automatic_production_id: UUID | None
    contract: WorkflowRunContract


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
    automatic_production_id: UUID | None = None


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
        return read_shot_content(value).model_dump(mode="json")


class EventRead(Resource):
    run_id: UUID
    event_type: str
    payload_json: dict
