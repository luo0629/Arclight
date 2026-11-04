"""Bounded public run/result contracts; credentials and payloads stay private."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arclight.domain.enums import RunStatus
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.model_gateway import GatewayStatus, GatewayUsage

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[\w.:-]+$")]
Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Money = Annotated[Decimal, Field(ge=0, lt=10**12, decimal_places=6)]
WarningCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]


class WorkflowContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InputReference(WorkflowContract):
    field: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}_(?:id|sha256)$")]
    value: UUID | Checksum

    @model_validator(mode="after")
    def matching_reference(self):
        if self.field.endswith("_id") != isinstance(self.value, UUID):
            raise ValueError("Reference type differs from field")
        return self


class RunInputSnapshot(WorkflowContract):
    origin: Literal["VERSION_BINDING", "LEGACY_RUN"]
    sha256: Checksum
    references: list[InputReference] = Field(max_length=4096)
    context_manifest_ids: list[UUID] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_references(self):
        identities = {(item.field, str(item.value)) for item in self.references}
        if len(identities) != len(self.references) or len(set(self.context_manifest_ids)) != len(
            self.context_manifest_ids
        ):
            raise ValueError("Duplicate input reference")
        return self


class ProductionComponent(WorkflowContract):
    id: UUID
    family: Literal["TEXT", "ART", "REVIEW", "MEDIA"]
    kind: Literal["AGENT", "PROMPT", "SCHEMA", "MODEL"]
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    digest: Checksum

    @model_validator(mode="after")
    def safe_labels(self):
        safe_metadata({"name": self.name, "version": self.version})
        return self


class RunModelVersion(WorkflowContract):
    provider: Identifier
    model: str = Field(min_length=1, max_length=160)
    supplier: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def safe_model(self):
        safe_metadata({"provider": self.provider, "model": self.model, "supplier": self.supplier})
        return self


class RunVersionSnapshot(WorkflowContract):
    state: Literal["BOUND", "LEGACY_UNBOUND", "INCOMPATIBLE"]
    workflow_version: str = Field(min_length=1, max_length=32)
    components: list[ProductionComponent] = Field(max_length=256)
    model: RunModelVersion | None
    parameter_sha256: Checksum | None

    @model_validator(mode="after")
    def unique_components(self):
        if len({item.id for item in self.components}) != len(self.components):
            raise ValueError("Duplicate component identity")
        if self.state == "BOUND" and (
            not self.components or self.model is None or self.parameter_sha256 is None
        ):
            raise ValueError("Bound versions require exact component and model evidence")
        if self.state == "LEGACY_UNBOUND" and (
            self.components or self.model is not None or self.parameter_sha256 is not None
        ):
            raise ValueError("Legacy records cannot invent component evidence")
        return self


class RunBudget(WorkflowContract):
    scope: Literal["project"] = "project"
    origin: Literal["STARTUP_CONTEXT", "UNKNOWN"]
    currency: Literal["USD"] = "USD"
    limit: Money | None
    task_limit: Money | None
    episode_limit: Money | None
    cost_estimate: Money | None


class GatewayCallUsage(WorkflowContract):
    call_id: UUID
    task: Identifier
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=160)
    status: GatewayStatus
    outcome_source: Literal["INITIAL", "RECONCILIATION", "MISSING"]
    quoted_cost: Money | None
    usage: GatewayUsage

    @model_validator(mode="after")
    def valid_usage(self):
        self.usage.validate_status(self.status)
        if self.outcome_source == "MISSING" and self.status != "RESULT_UNKNOWN":
            raise ValueError("Missing receipt cannot assert a final outcome")
        safe_metadata({"provider": self.provider, "model": self.model})
        return self


class RunUsage(WorkflowContract):
    currency: Literal["USD"] = "USD"
    total_calls: int = Field(strict=True, ge=0)
    calls: list[GatewayCallUsage] = Field(max_length=100)

    @model_validator(mode="after")
    def valid_count(self):
        if self.total_calls < len(self.calls) or len({item.call_id for item in self.calls}) != len(
            self.calls
        ):
            raise ValueError("Invalid call count or duplicate call")
        return self


class WorkflowResult(WorkflowContract):
    result_id: UUID
    project_id: UUID
    episode_id: UUID | None
    agent_name: Identifier | None
    schema_version: Literal["1.0"] = "1.0"
    status: RunStatus
    summary: str = Field(min_length=1, max_length=500)
    artifact_ids: list[UUID] = Field(max_length=1000)
    warnings: list[WarningCode] = Field(max_length=100)
    requires_human_review: bool = Field(strict=True)

    @model_validator(mode="after")
    def safe_result(self):
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Duplicate artifact identity")
        safe_metadata({"summary": self.summary, "agent_name": self.agent_name})
        return self


class WorkflowRunContract(WorkflowContract):
    schema_version: Literal[1] = 1
    run_id: UUID
    project_id: UUID
    episode_id: UUID | None
    requested_by: str | None = Field(min_length=1, max_length=100)
    task_type: Identifier
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    retry_count: int = Field(strict=True, ge=0)
    input_snapshot: RunInputSnapshot
    workflow_version: str = Field(min_length=1, max_length=32)
    versions: RunVersionSnapshot
    budget: RunBudget
    usage: RunUsage
    trace_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w-]+$")]
    status: RunStatus
    result: WorkflowResult | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @field_validator("requested_at", "started_at", "finished_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_run(self):
        safe_metadata(
            {
                "requested_by": self.requested_by,
                "task_type": self.task_type,
                "workflow_version": self.workflow_version,
                "trace_id": self.trace_id,
            }
        )
        if self.versions.workflow_version != self.workflow_version:
            raise ValueError("Mismatched workflow versions")
        if self.result and (
            self.result.project_id != self.project_id
            or self.result.episode_id != self.episode_id
            or self.result.status != self.status
        ):
            raise ValueError("Result scope or status differs from run")
        if self.finished_at is not None and self.status in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.WAITING_REVIEW,
        }:
            raise ValueError("Active runs cannot have a finish time")
        known = [
            value
            for value in (self.requested_at, self.started_at, self.finished_at)
            if value is not None
        ]
        if known != sorted(known):
            raise ValueError("Invalid run timestamp order")
        return self
