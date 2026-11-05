"""Versioned public call facts; transport credentials never enter these contracts."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, model_validator

from arclight.domain.agent_limits import AgentExecutionContext
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.media_capabilities import MediaCapabilities

GatewayStatus = Literal[
    "SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "RESULT_UNKNOWN", "NOT_ACCEPTED"
]


class GatewayValue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GatewayPolicy(GatewayValue):
    global_per_minute: int = Field(default=600, ge=1, le=100000)
    user_per_minute: int = Field(default=120, ge=1, le=100000)
    failure_threshold: int = Field(default=3, ge=1, le=100)
    cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    max_attempts: int = Field(default=1, ge=1, le=3)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    text_maximum_cost: Decimal | None = Field(default=None, ge=0, le=10000, decimal_places=6)
    pricing_version: str = Field(default="unconfigured", min_length=1, max_length=100)


class GatewayCapabilities(GatewayValue):
    version: str = Field(min_length=1, max_length=100)
    tasks: frozenset[str]
    parameters: frozenset[str] = frozenset()
    media: MediaCapabilities | None = None
    agent: AgentModelCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_serializer("tasks", "parameters")
    def sorted_values(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @field_serializer("media")
    def media_value(self, value: MediaCapabilities | None) -> dict | None:
        if value is None:
            return None
        result = value.model_dump(mode="json")
        result["task_types"] = sorted(value.task_types)
        return result


class GatewayRoute(GatewayValue):
    provider: str = Field(min_length=1, max_length=100, pattern=r"^[\w.:-]+$")
    model: str = Field(min_length=1, max_length=160)
    config_version: str = Field(min_length=1, max_length=100)
    credential_source: Literal["SERVER", "ACCOUNT_SNAPSHOT", "NONE"]
    capabilities: GatewayCapabilities
    maximum_cost: Decimal | None = Field(default=None, ge=0, lt=10**12, decimal_places=6)
    pricing_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def safe_route(self):
        safe_metadata(self.model_dump(mode="json", exclude={"credential_source"}))
        return self


class GatewayRequest(GatewayValue):
    schema_version: Literal[1] = 1
    project_id: UUID
    episode_id: UUID | None = None
    actor: str = Field(min_length=1, max_length=100)
    operation_key: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=40)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    workflow_id: UUID | None = None
    generation_attempt_id: UUID | None = None
    authorized_routes: frozenset[str] = frozenset()
    execution: AgentExecutionContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def safe_request(self):
        safe_metadata(self.parameters)
        if self.workflow_id is not None and self.generation_attempt_id is not None:
            raise ValueError("A call has one authoritative owner")
        return self


class GatewayUsage(GatewayValue):
    input_tokens: int | None = Field(default=None, strict=True, ge=0, le=10**10)
    output_tokens: int | None = Field(default=None, strict=True, ge=0, le=10**10)
    images: int | None = Field(default=None, strict=True, ge=0, le=10000)
    seconds: Decimal | None = Field(default=None, ge=0, le=10**10)
    actual_cost: Decimal | None = Field(default=None, ge=0, lt=10**12, decimal_places=6)

    def validate_status(self, status: str) -> None:
        if status not in {
            "SUBMITTED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "RESULT_UNKNOWN",
            "NOT_ACCEPTED",
        }:
            raise ValueError("Invalid model call status")
        if status in {"SUBMITTED", "RUNNING", "RESULT_UNKNOWN"} and any(
            value is not None for value in self.model_dump().values()
        ):
            raise ValueError("Unresolved calls cannot assert final usage")
        if status == "NOT_ACCEPTED" and (
            self.actual_cost != 0
            or any(
                value not in (None, 0)
                for key, value in self.model_dump().items()
                if key != "actual_cost"
            )
        ):
            raise ValueError("Non-acceptance requires zero usage and cost")


class GatewayFailure(Exception):
    """Only an adapter with explicit non-acceptance evidence may permit a retry."""

    def __init__(
        self,
        code: str,
        *,
        not_accepted: bool = False,
        transient: bool = False,
        completed: bool = False,
        usage: GatewayUsage | None = None,
    ):
        if not_accepted and (completed or usage is not None):
            raise ValueError("Non-acceptance cannot assert completed usage")
        if usage is not None and not completed:
            raise ValueError("Usage requires a completed provider response")
        self.code = code
        self.not_accepted = not_accepted
        self.transient = transient
        self.completed = completed
        self.usage = GatewayUsage.model_validate(usage.model_dump()) if usage else GatewayUsage()
        super().__init__("Model service call failed")
