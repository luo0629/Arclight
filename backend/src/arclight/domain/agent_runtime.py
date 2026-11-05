"""Contracts for a bounded decision loop, separate from domain publication and approval."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.agent_state import AgentStepId
from arclight.domain.agent_tools import AgentName
from arclight.domain.generation_requests import safe_metadata


class HumanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    reason: Literal["MISSING_EVIDENCE", "CONSTRAINT_CONFLICT", "LOW_CONFIDENCE", "USER_DECISION"]
    question: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded_safe_question(self):
        if any(not option.strip() or len(option) > 200 for option in self.options):
            raise ValueError("Decision options must be bounded, nonempty text")
        if not self.question.strip() or len(set(self.options)) != len(self.options):
            raise ValueError("A decision needs a question and unique options")
        safe_metadata(self.model_dump(mode="json"))
        return self


class AgentDecision[T: BaseModel](BaseModel):
    """The model may propose a delivery or ask a human; it cannot publish either."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    decision: Literal["COMPLETE", "HUMAN_REVIEW"]
    result: T | None
    human_request: HumanDecisionRequest | None

    @model_validator(mode="after")
    def exclusive_decision(self):
        if self.decision == "COMPLETE":
            if self.result is None or self.human_request is not None:
                raise ValueError("Completion requires only a validated delivery")
        elif self.result is not None or self.human_request is None:
            raise ValueError("Human review requires only a decision request")
        safe_metadata(self.model_dump(mode="json"))
        return self


class AgentLoopResult[T: BaseModel](BaseModel):
    """Server-scoped result; the owning workflow persists artifacts and review state."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    runtime_version: Literal["agent-runtime/1"] = "agent-runtime/1"
    run_id: UUID
    project_id: UUID
    agent_name: AgentName
    stage: AgentStepId
    status: Literal["COMPLETED", "HUMAN_REVIEW", "STOPPED"]
    model_turns: int = Field(strict=True, ge=0, le=4096)
    loop_id: UUID | None = None
    question_id: UUID | None = None
    input_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    result: T | None = None
    human_request: HumanDecisionRequest | None = None
    stop_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @model_validator(mode="after")
    def consistent_outcome(self):
        if (self.loop_id is not None) != (self.input_sha256 is not None) or (
            self.question_id is not None and (self.status != "HUMAN_REVIEW" or self.loop_id is None)
        ):
            raise ValueError("Durable decisions must identify their loop and frozen input")
        if (
            (self.result is not None) != (self.status == "COMPLETED")
            or (self.human_request is not None) != (self.status == "HUMAN_REVIEW")
            or (self.stop_code is not None) != (self.status == "STOPPED")
        ):
            raise ValueError("Loop status and outcome must agree")
        safe_metadata(self.model_dump(mode="json"))
        return self


class AgentHumanDecision(BaseModel):
    """An authenticated human answer, never a domain approval or a model instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    question_id: UUID
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["ANSWER", "STOP"]
    answer: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def consistent_answer(self):
        if (self.answer is not None) != (self.decision == "ANSWER"):
            raise ValueError("Only an answer decision contains answer text")
        if self.answer is not None and not self.answer.strip():
            raise ValueError("An answer must contain nonempty text")
        safe_metadata(self.model_dump(mode="json"))
        return self
