"""Bounded, content-safe single-turn Agent protocol, independent of a vendor wire format."""

import json
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, model_validator

from arclight.domain.agent_tools import AgentName, AgentToolCall, AgentToolFeedback, ToolName
from arclight.domain.generation_requests import safe_metadata

PROTOCOL_VERSION: Literal["agent-model/1"] = "agent-model/1"
CallId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]


@dataclass(frozen=True, slots=True)
class ContextImage:
    """Ephemeral transport data; persistent conversations contain only file IDs."""

    file_id: UUID
    media_type: str
    data: bytes = field(repr=False)


class AgentModelValue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class AgentModelCapabilities(AgentModelValue):
    """Explicit, model-specific server declaration; compatibility never implies vision/tools."""

    version: str = Field(min_length=1, max_length=100)
    supplier: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=160)
    tools: bool = False
    response_formats: frozenset[Literal["json_schema", "json_object"]] = frozenset()
    max_input_images: int = Field(default=0, strict=True, ge=0, le=8)
    max_image_bytes: int = Field(default=1048576, strict=True, ge=1, le=1048576)
    max_total_image_bytes: int = Field(default=4194304, strict=True, ge=1, le=8388608)

    @field_serializer("response_formats")
    def sorted_formats(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


class ModelToolCall(AgentModelValue):
    call_id: CallId
    tool_name: ToolName
    arguments: dict[str, JsonValue] = Field(default_factory=dict, repr=False)

    def tool_request(self, run_id: UUID) -> AgentToolCall:
        return AgentToolCall(
            run_id=run_id,
            tool_name=self.tool_name,
            idempotency_key=self.call_id,
            arguments=self.arguments,
        )


class AgentUserMessage(AgentModelValue):
    role: Literal["user"] = "user"
    data: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    image_file_ids: list[UUID] = Field(default_factory=list, max_length=8)


class AgentAssistantMessage(AgentModelValue):
    role: Literal["assistant"] = "assistant"
    tool_calls: list[ModelToolCall] = Field(min_length=1, max_length=16, repr=False)


class AgentToolMessage(AgentModelValue):
    role: Literal["tool"] = "tool"
    call_id: CallId
    feedback: AgentToolFeedback = Field(repr=False)


AgentMessage = Annotated[
    AgentUserMessage | AgentAssistantMessage | AgentToolMessage, Field(discriminator="role")
]


class AgentModelRequest(AgentModelValue):
    protocol_version: Literal["agent-model/1"] = PROTOCOL_VERSION
    run_id: UUID
    agent_name: AgentName
    stage: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    operation_key: str = Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")
    agent_version: str = Field(min_length=1, max_length=100)
    prompt_version: str = Field(min_length=1, max_length=100)
    result_schema_version: str = Field(min_length=1, max_length=100)
    system_instruction: str = Field(min_length=1, max_length=20000, repr=False)
    evidence_round: int = Field(default=0, strict=True, ge=0, le=32)
    max_tokens: int = Field(default=4096, strict=True, ge=1, le=16000)
    messages: list[AgentMessage] = Field(min_length=1, max_length=128, repr=False)

    @model_validator(mode="after")
    def bounded_paired_history(self):
        value = self.model_dump(mode="json")
        safe_metadata(value)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 262144:
            raise ValueError("Agent conversation exceeds its byte limit")
        if not isinstance(self.messages[0], AgentUserMessage):
            raise ValueError("Agent conversation must start with user data")
        pending: dict[str, ModelToolCall] = {}
        seen: set[str] = set()
        for message in self.messages:
            if isinstance(message, AgentToolMessage):
                call = pending.pop(message.call_id, None)
                feedback = message.feedback
                if call is None or (
                    feedback.run_id != self.run_id
                    or feedback.tool_name != call.tool_name
                    or feedback.idempotency_key != call.call_id
                ):
                    raise ValueError("Tool feedback must match its run, call ID and tool")
            else:
                if pending:
                    raise ValueError("Every tool call requires feedback before another message")
                if isinstance(message, AgentAssistantMessage):
                    for call in message.tool_calls:
                        call.tool_request(self.run_id)
                        if call.call_id in seen:
                            raise ValueError("Tool call IDs must be unique across the conversation")
                        seen.add(call.call_id)
                        pending[call.call_id] = call
        if pending:
            raise ValueError("Cannot invoke a model with unresolved tool calls")
        return self

    def image_ids(self) -> list[UUID]:
        return [
            identity
            for message in self.messages
            if isinstance(message, AgentUserMessage)
            for identity in message.image_file_ids
        ]


class AgentToolDeclaration(AgentModelValue):
    name: ToolName
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict = Field(repr=False)


class AgentModelResponse[T: BaseModel](AgentModelValue):
    protocol_version: Literal["agent-model/1"] = PROTOCOL_VERSION
    status: Literal["TOOL_CALLS", "FINAL"]
    tool_calls: list[ModelToolCall] = Field(default_factory=list, max_length=16, repr=False)
    final: T | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def exclusive_result(self):
        if self.status == "TOOL_CALLS":
            if not self.tool_calls or self.final is not None:
                raise ValueError("Tool selection and final output are mutually exclusive")
        elif self.tool_calls or self.final is None:
            raise ValueError("Final output requires a validated result")
        safe_metadata(self.model_dump(mode="json"))
        return self
