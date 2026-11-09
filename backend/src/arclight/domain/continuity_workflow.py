"""Exact, opt-in shot binding and durable Reviewer work identities."""

from uuid import UUID

from pydantic import Field

from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.continuity_reviews import ReviewRerun, ReviewTarget
from arclight.domain.generation_requests import MediaInput, ShotReviewBinding

REVIEW_WORKFLOW_VERSION = "continuity-review/1"
REVIEW_WORK_RESOURCE = "CONTINUITY_WORK"
REVIEW_BLOCK_RESOURCE = "CONTINUITY_BLOCK"


class CandidateReviewCreate(ShotReviewBinding):
    agent_limits: AgentRunLimits = Field(default_factory=lambda: AgentRunLimits(maximum_cost=None))


class CandidateReviewWork(MediaInput):
    run_id: UUID
    candidate_id: UUID
    revision_id: UUID | None = None
    target: ReviewTarget
    rerun: ReviewRerun | None = None
