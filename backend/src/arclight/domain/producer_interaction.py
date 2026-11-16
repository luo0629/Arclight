"""Public, bounded Producer controls; executable tool arguments remain server owned."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.domain.producer_planning import DelegationRequest


class ProducerRunCreate(AgentToolContract):
    goal: str = Field(min_length=1, max_length=4000)
    limits: AgentRunLimits = Field(
        default_factory=lambda: AgentRunLimits(max_duration_seconds=3600, maximum_cost=None)
    )

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A production goal must contain text")
        return value


class ProducerDelegateCommand(DelegationRequest):
    action: Literal["DELEGATE"]
    expected_revision: int = Field(strict=True, ge=0)


class ProducerReplanCommand(AgentToolContract):
    action: Literal["REPLAN"]
    expected_revision: int = Field(strict=True, ge=0)
    observation: ProducerObservationRequest


class ProducerRecoverCommand(AgentToolContract):
    action: Literal["RECOVER"]
    expected_revision: int = Field(strict=True, ge=0)
    command_id: UUID


class ProducerCancelCommand(AgentToolContract):
    action: Literal["CANCEL"]
    expected_revision: int = Field(strict=True, ge=0)


ProducerCommand = Annotated[
    ProducerDelegateCommand
    | ProducerReplanCommand
    | ProducerRecoverCommand
    | ProducerCancelCommand,
    Field(discriminator="action"),
]
