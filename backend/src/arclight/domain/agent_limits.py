"""Server-owned limits and action identities for one bounded Agent execution family."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arclight.domain.agent_state import AgentStepId
from arclight.domain.agent_tools import AgentName, ToolName


class AgentRunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1] = 1
    max_steps: int = Field(default=32, strict=True, ge=0, le=1024)
    max_calls: int = Field(default=128, strict=True, ge=0, le=4096)
    max_model_calls: int = Field(default=32, strict=True, ge=0, le=4096)
    max_tool_calls: int = Field(default=96, strict=True, ge=0, le=4096)
    max_agent_calls: int = Field(default=128, strict=True, ge=0, le=4096)
    max_duration_seconds: int = Field(default=300, strict=True, ge=1, le=86400)
    max_delegations: int = Field(default=8, strict=True, ge=0, le=128)
    max_delegation_depth: int = Field(default=2, strict=True, ge=0, le=8)
    maximum_cost: Decimal | None = Field(default=Decimal("0"), ge=0, lt=10**12, decimal_places=6)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("Agent limits schema_version must be an integer")
        return value


class AgentExecutionContext(BaseModel):
    """Supplied by the executor, never copied from model-generated tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    agent_name: AgentName
    stage: AgentStepId
    step_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: Literal["MODEL", "TOOL"] = "MODEL"
    tool_name: ToolName | None = None

    @model_validator(mode="after")
    def tool_identity(self):
        if (self.kind == "TOOL") != (self.tool_name is not None):
            raise ValueError("Only a tool action has a registered tool identity")
        return self
