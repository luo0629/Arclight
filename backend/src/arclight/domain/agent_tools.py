"""Bounded, versioned contracts for server-registered Agent tools."""

import json
import re
from collections.abc import Sequence
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from arclight.domain.generation_requests import safe_metadata

AgentName = Literal["producer", "screenwriter", "director", "art_director", "continuity_reviewer"]
ToolName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")]


class AgentToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AgentToolCall(AgentToolContract):
    run_id: UUID
    tool_name: ToolName
    idempotency_key: IdempotencyKey
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def bounded_untrusted_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        safe_metadata(value)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 32768:
            raise ValueError("Agent tool arguments exceed the size limit")
        return value


class AgentToolGrantRequest(AgentToolContract):
    run_id: UUID
    agent_name: AgentName
    allowed_tools: list[ToolName] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_tools(self):
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("Duplicate Agent tool grant")
        return self


class ReadStoryEntryParameters(AgentToolContract):
    entry_id: UUID


class ReadStoryEntryResult(AgentToolContract):
    entry_id: UUID
    project_id: UUID
    kind: Literal["WORLD", "CHARACTER", "LOCATION", "PROP", "SCENE"]
    name: str = Field(min_length=1, max_length=100)
    revision: int = Field(strict=True, ge=0)
    data: dict[str, JsonValue]


class AgentToolError(AgentToolContract):
    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]
    message: Annotated[str, Field(min_length=1, max_length=300)]
    retryable: bool = False
    fields: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=16
    )

    @field_validator("message")
    @classmethod
    def safe_message(cls, value: str) -> str:
        safe_metadata(value)
        return value


class AgentToolFeedback(AgentToolContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    tool_name: ToolName
    idempotency_key: IdempotencyKey
    status: Literal["SUCCEEDED", "FAILED"]
    trust: Literal["UNTRUSTED"] = "UNTRUSTED"
    result: JsonValue | None = None
    error: AgentToolError | None = None

    @model_validator(mode="after")
    def consistent_feedback(self):
        if (self.status == "SUCCEEDED") != (self.error is None):
            raise ValueError("Tool feedback status and error must agree")
        if (self.status == "SUCCEEDED") != (self.result is not None):
            raise ValueError("Tool feedback status and result must agree")
        if self.result is not None:
            safe_metadata(self.result)
            if len(json.dumps(self.result, ensure_ascii=False, allow_nan=False).encode()) > 65536:
                raise ValueError("Agent tool result exceeds the size limit")
        return self


def validation_fields(errors: Sequence[object]) -> list[str]:
    """Return field paths only; never echo untrusted parameter values to the Agent."""
    fields: list[str] = []
    for error in errors[:16]:
        location_parts = error.get("loc", ()) if isinstance(error, dict) else ()
        location = ".".join(str(part) for part in location_parts)
        normalized = re.sub(r"[^a-zA-Z0-9_.-]", "", location)[:160]
        if normalized and normalized not in fields:
            fields.append(normalized)
    return fields
