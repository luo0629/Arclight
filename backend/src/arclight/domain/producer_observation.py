"""Immutable, bounded observations of authoritative Producer state."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from arclight.domain.agent_runtime import AgentHumanDecision, HumanDecisionRequest
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_proposals import Checksum
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.producer_planning import DelegationRequest, PlanText, TaskKey
from arclight.domain.producer_research import InvestigateProjectResult
from arclight.domain.production_events import StageReceipt

ObservationTrigger = Literal["FAILURE", "STALE", "BUDGET_CHANGED", "HUMAN_DECISION", "RECHECK"]


class ProducerObservationRequest(AgentToolContract):
    base_plan_id: UUID
    base_plan_sha256: Checksum
    trigger: ObservationTrigger
    reason: PlanText
    source_event_id: UUID | None = None
    human_decision_id: UUID | None = None
    requested_task_keys: list[TaskKey] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def checked_request(self):
        if len(set(self.requested_task_keys)) != len(self.requested_task_keys):
            raise ValueError("Requested task keys must be unique")
        if self.trigger == "HUMAN_DECISION" and self.human_decision_id is None:
            raise ValueError("A human trigger requires a saved human decision")
        return self


class ObservedProducerTask(AgentToolContract):
    task_key: TaskKey
    origin: DelegationRequest
    child_run_id: UUID | None = None
    status: str | None = Field(default=None, max_length=32)
    revision: StrictInt | None = Field(default=None, ge=0)
    input_error_code: str | None = Field(default=None, max_length=64)
    output_version_ids: list[UUID] = Field(default_factory=list, max_length=128)
    output_sha256: Checksum
    unknown_external_result: StrictBool
    replaceable: StrictBool


class ProducerAgentHumanEvidence(AgentToolContract):
    kind: Literal["AGENT"] = "AGENT"
    decision_id: UUID
    source_run_id: UUID
    actor_id: str = Field(min_length=1, max_length=160)
    loop_id: UUID
    question_id: UUID
    input_sha256: Checksum
    question: HumanDecisionRequest
    decision: AgentHumanDecision


class ProducerFormalHumanEvidence(AgentToolContract):
    kind: Literal["FORMAL"] = "FORMAL"
    decision_id: UUID
    source_run_id: UUID
    actor_id: str = Field(min_length=1, max_length=160)
    request_id: UUID
    request_sha256: Checksum
    snapshot: ReviewSnapshot
    decision: HumanDecision
    resume_id: UUID | None = None


ProducerHumanEvidence = Annotated[
    ProducerAgentHumanEvidence | ProducerFormalHumanEvidence, Field(discriminator="kind")
]


class ProducerObservation(AgentToolContract):
    schema_version: Literal["producer-observation/1"] = "producer-observation/1"
    id: UUID
    run_id: UUID
    project_id: UUID
    episode_id: UUID
    base_plan_id: UUID
    base_plan_sha256: Checksum
    version_no: StrictInt = Field(ge=1)
    trigger: ObservationTrigger
    reason: PlanText
    read_at: datetime
    snapshot_sha256: Checksum
    authority_sha256: Checksum
    overview: InvestigateProjectResult
    reviews: InvestigateProjectResult
    tasks: list[ObservedProducerTask] = Field(min_length=1, max_length=16)
    affected_task_keys: list[TaskKey] = Field(default_factory=list, max_length=16)
    source_event_id: UUID | None = None
    human_decision_id: UUID | None = None
    human_evidence: ProducerHumanEvidence | None = None
    stage_receipt: StageReceipt | None = None

    @model_validator(mode="after")
    def checked_scope(self):
        if (self.human_evidence is None) != (self.human_decision_id is None):
            raise ValueError("Human observation requires its saved evidence")
        if (
            self.human_evidence is not None
            and self.human_evidence.decision_id != self.human_decision_id
        ):
            raise ValueError("Human observation evidence must identify the saved decision")
        keys = [task.task_key for task in self.tasks]
        if len(keys) != len(set(keys)) or not set(self.affected_task_keys) <= set(keys):
            raise ValueError("Observation task scope is inconsistent")
        if len(self.affected_task_keys) != len(set(self.affected_task_keys)):
            raise ValueError("Affected task keys must be unique")
        for task in self.tasks:
            if task.replaceable and task.task_key not in self.affected_task_keys:
                raise ValueError("Only affected tasks can be replaced")
        return self
