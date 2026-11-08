"""Immutable human escalation facts, separate from a completed review or admission."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.continuity_reviews import Checksum, ReviewTarget
from arclight.domain.production_state import ProductionContract

ESCALATION_RESOURCE = "REVIEW_ESCALATION"
INCOMPLETE_REVIEW_HANDLER = "continuity_incomplete"
LIMIT_STOP_CODES = frozenset(
    {
        "AGENT_MODEL_CALL_LIMIT",
        "AGENT_TOOL_CALL_LIMIT",
        "AGENT_CALL_LIMIT",
        "AGENT_AGENT_CALL_LIMIT",
        "AGENT_STEP_LIMIT",
        "AGENT_CONTEXT_LIMIT",
        "AGENT_COST_LIMIT",
        "AGENT_BUDGET_EXCEEDED",
        "AGENT_DEADLINE_EXCEEDED",
    }
)


class ContinuityEscalation(ProductionContract):
    schema_version: Literal["continuity-escalation/1"] = "continuity-escalation/1"
    project_id: UUID
    episode_id: UUID
    run_id: UUID
    loop_id: UUID
    thread_id: str
    target: ReviewTarget
    requested_by: str
    reason: Literal["HUMAN_REVIEW", "LIMIT_EXHAUSTED"]
    reasons: list[str] = Field(min_length=1, max_length=20)
    review_id: UUID | None
    input_sha256: Checksum
    outcome_sha256: Checksum
    source_version_ids: list[UUID] = Field(max_length=128)
    stop_code: str | None = None

    @model_validator(mode="after")
    def exact_origin(self):
        if self.thread_id != f"agent-loop:{self.loop_id}":
            raise ValueError("Escalation must retain the original reviewer thread")
        if self.reason == "HUMAN_REVIEW":
            if self.review_id is None or self.stop_code is not None:
                raise ValueError("Completed escalation requires its exact review")
        elif self.review_id is not None or self.stop_code not in LIMIT_STOP_CODES:
            raise ValueError("Incomplete escalation must retain a real exhausted stop")
        return self
