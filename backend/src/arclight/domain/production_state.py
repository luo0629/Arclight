"""Internal producer evidence and public, derived production states."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from arclight.domain.enums import AssetStatus
from arclight.domain.workflow_contracts import WarningCode, WorkflowContract


class EpisodePhase(StrEnum):
    DRAFT = "DRAFT"
    SCRIPTING = "SCRIPTING"
    WAITING_SCRIPT_REVIEW = "WAITING_SCRIPT_REVIEW"
    ASSET_PREPARATION = "ASSET_PREPARATION"
    STORYBOARDING = "STORYBOARDING"
    WAITING_STORYBOARD_REVIEW = "WAITING_STORYBOARD_REVIEW"
    GENERATING_MEDIA = "GENERATING_MEDIA"
    MEDIA_QA = "MEDIA_QA"
    EDITING = "EDITING"
    RENDERING = "RENDERING"
    DELIVERY_QA = "DELIVERY_QA"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ShotPhase(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    READY = "READY"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    QA_PENDING = "QA_PENDING"
    CONTINUITY_REVIEW = "CONTINUITY_REVIEW"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    RETRY_WAIT = "RETRY_WAIT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProductionContract(WorkflowContract):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvidenceScope(ProductionContract):
    project_id: UUID
    episode_id: UUID | None


class VersionEvidence(EvidenceScope):
    version_id: UUID
    kind: Literal["SCRIPT", "STORYBOARD", "IMAGE", "TIMELINE", "RENDER", "RENDER_PREVIEW"]
    status: AssetStatus
    stale: bool = Field(default=False, strict=True)
    source_version_ids: list[UUID] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_version_ids)) != len(self.source_version_ids):
            raise ValueError("Duplicate production source version")
        if self.version_id in self.source_version_ids:
            raise ValueError("Production version cannot depend on itself")
        return self

    @property
    def approved(self) -> bool:
        return not self.stale and self.status in {AssetStatus.APPROVED, AssetStatus.LOCKED}


ExecutionStatus = Literal[
    "CREATED",
    "QUEUED",
    "SUBMITTING",
    "SUBMITTED",
    "RUNNING",
    "RETRY_WAIT",
    "SUCCEEDED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "RESULT_UNKNOWN",
    "RECONCILING",
    "MANUAL_REVIEW",
    "WAITING_REVIEW",
    "REJECTED",
]


class ExecutionEvidence(EvidenceScope):
    episode_id: UUID
    execution_id: UUID
    target_id: UUID
    status: ExecutionStatus


class ReviewEvidence(EvidenceScope):
    episode_id: UUID
    review_id: UUID
    version_id: UUID
    source_version_id: UUID
    actor_id: str = Field(min_length=1, max_length=100)
    decision: Literal["PASS", "APPROVE", "REJECT", "REVISION_REQUIRED", "HUMAN_REVIEW"]


class WarningAcceptance(ReviewEvidence):
    warnings: list[WarningCode] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_warnings(self):
        if len(set(self.warnings)) != len(self.warnings):
            raise ValueError("Duplicate confirmed warning")
        return self


IssueCode = Literal[
    "MISSING_SCRIPT",
    "SCRIPT_NOT_APPROVED",
    "MISSING_STORYBOARD",
    "STORYBOARD_NOT_APPROVED",
    "SOURCE_VERSION_MISMATCH",
    "STALE_INPUT",
    "MISSING_ASSET",
    "ASSET_NOT_APPROVED",
    "INVALID_CONTENT",
    "MISSING_SHOTS",
    "MISSING_MEDIA",
    "MEDIA_CHECK_PENDING",
    "MEDIA_CHECK_FAILED",
    "CONTINUITY_PENDING",
    "CONTINUITY_REVISION",
    "HUMAN_REVIEW_REQUIRED",
    "SELECTION_PENDING",
    "SELECTION_REJECTED",
    "EXECUTION_FAILED",
    "EXECUTION_CANCELLED",
    "RESULT_UNCERTAIN",
    "MISSING_TIMELINE",
    "MISSING_RENDER",
    "DELIVERY_REVIEW_PENDING",
    "WARNINGS_NOT_CONFIRMED",
    "MISSING_VERSION_REGISTRY",
    "PRODUCER_NOT_CONNECTED",
]


class ShotFacts(EvidenceScope):
    episode_id: UUID
    shot_id: UUID
    storyboard_version_id: UUID
    issues: list[IssueCode] = Field(default_factory=list, max_length=100)
    execution: ExecutionEvidence | None = None
    media: VersionEvidence | None = None
    file_check: ReviewEvidence | None = None
    continuity: ReviewEvidence | None = None
    selection: ReviewEvidence | None = None


class EpisodeFacts(EvidenceScope):
    episode_id: UUID
    archived: bool = Field(default=False, strict=True)
    script: VersionEvidence | None = None
    storyboard: VersionEvidence | None = None
    input_issues: list[IssueCode] = Field(default_factory=list, max_length=100)
    script_execution: ExecutionEvidence | None = None
    storyboard_execution: ExecutionEvidence | None = None
    shots: list[ShotFacts] = Field(default_factory=list, max_length=2000)
    media_executions: list[ExecutionEvidence] = Field(default_factory=list, max_length=2000)
    # Existing generic media jobs have episode scope, but no authoritative shot binding.
    # Aggregate their history in SQL without truncation or treating exploratory jobs as required.
    unbound_media_counts: dict[ExecutionStatus, Annotated[int, Field(strict=True, ge=1)]] = Field(
        default_factory=dict, max_length=16
    )
    timeline: VersionEvidence | None = None
    render: VersionEvidence | None = None
    render_validation: ReviewEvidence | None = None
    delivery_review: ReviewEvidence | None = None
    warnings: list[WarningCode] = Field(default_factory=list, max_length=100)
    warning_acceptance: WarningAcceptance | None = None
    unavailable_producers: list[Literal["MEDIA_QA", "TIMELINE", "RENDER", "DELIVERY_QA"]] = Field(
        default_factory=list, max_length=4
    )

    @model_validator(mode="after")
    def scoped_evidence(self):
        def check(value: EvidenceScope):
            if value.project_id != self.project_id or value.episode_id not in {
                None,
                self.episode_id,
            }:
                raise ValueError("Production evidence belongs to a different scope")

        for value in (
            self.script,
            self.storyboard,
            self.script_execution,
            self.storyboard_execution,
            self.timeline,
            self.render,
            self.render_validation,
            self.delivery_review,
            self.warning_acceptance,
            *self.media_executions,
        ):
            if value is not None:
                check(value)
        for value, kind in (
            (self.script, "SCRIPT"),
            (self.storyboard, "STORYBOARD"),
            (self.timeline, "TIMELINE"),
            (self.render, "RENDER"),
        ):
            if value is not None and value.kind != kind:
                raise ValueError("Production stage needs the exact output kind")
            if value is not None and value.episode_id != self.episode_id:
                raise ValueError("Production stage needs the exact episode")
        for shot in self.shots:
            check(shot)
            if shot.episode_id != self.episode_id:
                raise ValueError("Shot evidence needs the exact episode")
            for value in (
                shot.execution,
                shot.media,
                shot.file_check,
                shot.continuity,
                shot.selection,
            ):
                if value is not None:
                    check(value)
            if shot.media is not None and shot.media.kind != "IMAGE":
                raise ValueError("Shot media needs an image version")
        for values, attribute in (
            (self.shots, "shot_id"),
            (self.media_executions, "execution_id"),
        ):
            ids = [getattr(item, attribute) for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate production identity")
        if len(set(self.warnings)) != len(self.warnings):
            raise ValueError("Duplicate production warning")
        return self


class ShotProductionState(ProductionContract):
    shot_id: UUID
    storyboard_version_id: UUID
    phase: ShotPhase
    media_version_id: UUID | None
    issues: list[IssueCode]


ConditionName = Literal[
    "SCRIPT_APPROVED",
    "ASSETS_USABLE",
    "STORYBOARD_APPROVED",
    "SHOTS_APPROVED",
    "TIMELINE_READY",
    "RENDER_READY",
    "DELIVERY_APPROVED",
    "WARNINGS_CONFIRMED",
]
ProducerKind = Literal["MEDIA_QA", "TIMELINE", "RENDER", "DELIVERY_QA"]


class StageCondition(ProductionContract):
    condition: ConditionName
    satisfied: bool
    issues: list[IssueCode]


class EpisodeProductionState(ProductionContract):
    schema_version: Literal["production-state/1"] = "production-state/1"
    project_id: UUID
    episode_id: UUID
    archived: bool
    phase: EpisodePhase
    blocked_phase: EpisodePhase | None
    script_version_id: UUID | None
    storyboard_version_id: UUID | None
    conditions: list[StageCondition]
    issues: list[IssueCode]
    warnings: list[WarningCode]
    warnings_confirmed: bool
    shot_count: int
    approved_shot_count: int
    shot_counts: dict[ShotPhase, int]
    task_counts: dict[str, int]
    unbound_task_counts: dict[str, int]
    task_issues: list[IssueCode]
    has_partial_failure: bool
    unavailable_producers: list[ProducerKind]
    evidence_sha256: str
    shots: list[ShotProductionState]
