"""Public, bounded Producer controls; executable tool arguments remain server owned."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.domain.producer_planning import DelegationRequest
from arclight.domain.workflow_contracts import Checksum

VOICE_CASTING_ACTION: Literal["ASSIGN_VOICES"] = "ASSIGN_VOICES"


class ProducerRunCreate(AgentToolContract):
    goal: str = Field(min_length=1, max_length=4000)

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


class ProducerVoiceCastingCommand(AgentToolContract):
    """The short-drama scheduler alone can enqueue this command."""

    action: Literal["ASSIGN_VOICES"] = VOICE_CASTING_ACTION
    casting_sha256: Checksum


ProducerCommand = Annotated[
    ProducerDelegateCommand
    | ProducerReplanCommand
    | ProducerRecoverCommand
    | ProducerCancelCommand,
    Field(discriminator="action"),
]
