"""Bounded metadata observations for the Producer's existing tool gateway."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_serializer, field_validator

from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.costs import CostTotals
from arclight.domain.production_state import (
    EpisodePhase,
    IssueCode,
    ProducerKind,
    ShotPhase,
    StageCondition,
)
from arclight.domain.workflow_contracts import WarningCode

RESEARCH_TOOL = "investigate_project"


class ProducerOverviewQuery(AgentToolContract):
    path: Literal["overview"]
    episode_id: UUID | None = None


class ProducerBudgetQuery(AgentToolContract):
    path: Literal["budget"]
    episode_id: UUID | None = None


class ProducerReviewQuery(AgentToolContract):
    path: Literal["reviews"]
    episode_id: UUID | None = None
    version_ids: list[UUID] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=20, strict=True, ge=1, le=20)
    offset: int = Field(default=0, strict=True, ge=0)

    @field_validator("version_ids")
    @classmethod
    def unique_versions(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("精确版本不能重复")
        return value


class ProducerDependencyQuery(AgentToolContract):
    path: Literal["dependencies"]
    version_ids: list[UUID] = Field(min_length=1, max_length=8)
    direction: Literal["incoming", "outgoing"] = "incoming"
    limit: int = Field(default=20, strict=True, ge=1, le=20)
    offset: int = Field(default=0, strict=True, ge=0)

    @field_validator("version_ids")
    @classmethod
    def unique_versions(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("精确版本不能重复")
        return value


ProducerQuery = Annotated[
    ProducerOverviewQuery | ProducerBudgetQuery | ProducerReviewQuery | ProducerDependencyQuery,
    Field(discriminator="path"),
]


class InvestigateProjectParameters(AgentToolContract):
    query: ProducerQuery
    expected_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", strict=True
    )


class ProducerReadVersion(AgentToolContract):
    kind: str
    id: UUID
    revision: int | None = None
    version_no: int | None = None
    status: str | None = None
    checksum: str | None = None
    updated_at: datetime | None = None


class ProducerInformationGap(AgentToolContract):
    code: str
    source_id: UUID | None = None


class ProducerProductionSummary(AgentToolContract):
    schema_version: Literal["production-state/1"]
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


class ProducerRuntimeBudget(AgentToolContract):
    """Recorded usage across the producer run and its children."""

    root_run_id: UUID
    calls: int
    model_calls: int
    tool_calls: int
    actual_cost: Decimal
    pending_estimate: Decimal
    unknown_cost: bool

    @field_serializer("actual_cost", "pending_estimate")
    def money(self, value: Decimal) -> str:
        return format(value, ".6f")


class ProducerBudget(AgentToolContract):
    project_costs: CostTotals
    episode_costs: CostTotals


class ProducerVersion(AgentToolContract):
    id: UUID
    asset_id: UUID
    asset_type: str
    version_no: int
    checksum: str
    status: str
    revision: int
    stale: bool
    current_pointer: bool
    usable: bool
    manifest_id: UUID | None = None


class ProducerDependencyEdge(AgentToolContract):
    id: UUID
    source_version_id: UUID
    target_version_id: UUID
    dependency_type: str
    manifest_id: UUID | None
    source_usable: bool


class ProducerDependencyInputEvidence(AgentToolContract):
    """Exact manifest index and a digest of every checked, potentially unexpanded input."""

    target_version_id: UUID
    manifest_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_count: int
    unexpanded_input_count: int
    registered_input_count: int
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issues: dict[str, int]


class ProducerDependencies(AgentToolContract):
    selected_versions: list[ProducerVersion] = Field(max_length=8)
    edges: list[ProducerDependencyEdge] = Field(max_length=20)
    input_evidence: list[ProducerDependencyInputEvidence] = Field(
        default_factory=list, max_length=8
    )
    has_more: bool
    limit: int
    offset: int


class ProducerPendingReview(AgentToolContract):
    id: UUID
    source: str
    episode_id: UUID | None
    kind: str
    object_id: UUID
    asset_id: UUID | None
    version_id: UUID | None
    run_id: UUID | None
    status: str
    blocking_code: str | None
    created_at: datetime


class ProducerReviewDecision(AgentToolContract):
    id: UUID
    run_id: UUID
    resource_type: str
    version_id: UUID
    reviewer_id: str
    decision: str
    created_at: datetime


class ProducerReviews(AgentToolContract):
    pending: list[ProducerPendingReview]
    selected_versions: list[ProducerVersion]
    decisions: list[ProducerReviewDecision]
    total: int
    has_more: bool
    limit: int
    offset: int


class InvestigateProjectResult(AgentToolContract):
    schema_version: Literal["producer-research/1"] = "producer-research/1"
    query: ProducerQuery
    project_id: UUID
    episode_id: UUID
    read_at: datetime
    # 2 scope rows + 2 * (8 selected + 20 other edge endpoints) + 8 manifests.
    read_versions: list[ProducerReadVersion] = Field(max_length=66)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    production: ProducerProductionSummary | None = None
    budget: ProducerBudget | None = None
    runtime_budget: ProducerRuntimeBudget | None = None
    dependencies: ProducerDependencies | None = None
    reviews: ProducerReviews | None = None
    project_episode_count: int | None = None
    missing: list[ProducerInformationGap] = Field(default_factory=list, max_length=72)
    unknown: list[ProducerInformationGap] = Field(default_factory=list, max_length=16)
