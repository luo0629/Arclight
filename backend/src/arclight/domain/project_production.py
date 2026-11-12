"""Bounded, public read models for the existing project production ledgers."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from arclight.domain.render_deliveries import RenderDeliveryRead

TaskFamily = Literal["WORKFLOW", "GENERATION", "RENDER"]
TaskType = Literal["TEXT", "IMAGE", "AUDIO", "VIDEO", "RENDER", "OTHER"]
TaskStatus = Literal[
    "QUEUED",
    "RUNNING",
    "WAITING_REVIEW",
    "SUCCEEDED",
    "REJECTED",
    "FAILED",
    "CANCELLED",
    "CREATED",
    "SUBMITTING",
    "SUBMITTED",
    "RETRY_WAIT",
    "CANCEL_REQUESTED",
    "RESULT_UNKNOWN",
    "RECONCILING",
    "MANUAL_REVIEW",
    "UNKNOWN",
]
WorkflowType = Literal[
    "script_storyboard",
    "script",
    "storyboard",
    "script_rewrite",
    "story_analysis",
    "story_outline",
    "art_proposal",
    "continuity_review",
    "unknown",
]
PublicError = Literal[
    "INPUT_INVALID",
    "PROVIDER_REJECTED",
    "PROVIDER_FAILED",
    "PROVIDER_UNAVAILABLE",
    "RESULT_UNKNOWN",
    "STORAGE_FAILED",
    "CANCELLED",
    "BUDGET_EXCEEDED",
    "QUALITY_FAILED",
    "RENDER_FAILED",
    "RENDER_TIMEOUT",
    "RENDER_INTERRUPTED",
    "RENDER_INPUT_INVALID",
    "RENDER_OUTPUT_INVALID",
    "INSPECTION_FAILED",
    "WORKFLOW_FAILED",
    "INTERNAL_ERROR",
]
ReviewSource = Literal["HUMAN_REVIEW", "WORKFLOW", "SCRIPT", "STORYBOARD", "OUTLINE"]
ReviewKind = Literal[
    "SCRIPT",
    "STORYBOARD",
    "OUTLINE",
    "REFERENCE",
    "VISUAL_SPEC",
    "EXCEPTIONAL_SHOT",
    "EXPORT",
    "DELIVERY",
    "WORKFLOW",
]
ReviewBlocker = Literal[
    "PROJECT_ARCHIVED",
    "EPISODE_ARCHIVED",
    "REVIEW_INPUT_CHANGED",
    "REVIEW_SCOPE_INVALID",
    "REVIEW_HANDLER_UNAVAILABLE",
    "NOT_FOUND",
    "REVIEW_UNAVAILABLE",
]


class ProductionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectTask(ProductionRead):
    id: UUID
    family: TaskFamily
    project_id: UUID
    episode_id: UUID | None
    episode_no: int | None
    episode_title: str | None
    task_type: TaskType
    workflow_type: WorkflowType | None
    status: TaskStatus
    error_code: PublicError | None
    created_at: datetime
    updated_at: datetime
    detail_url: str

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProjectTaskPage(ProductionRead):
    items: list[ProjectTask]
    total: int
    limit: int
    offset: int


class PendingProjectReview(ProductionRead):
    id: UUID
    source: ReviewSource
    project_id: UUID
    episode_id: UUID | None
    episode_no: int | None
    episode_title: str | None
    kind: ReviewKind
    object_id: UUID
    asset_id: UUID | None
    version_id: UUID | None
    run_id: UUID | None
    status: Literal["WAITING_REVIEW", "BLOCKED"]
    blocking_code: ReviewBlocker | None
    created_at: datetime
    detail_url: str

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PendingProjectReviewPage(ProductionRead):
    items: list[PendingProjectReview]
    total: int
    limit: int
    offset: int


class ProjectDelivery(RenderDeliveryRead):
    episode_no: int
    episode_title: str
    detail_url: str


class ProjectDeliveryPage(ProductionRead):
    items: list[ProjectDelivery]
    total: int
    limit: int
    offset: int
