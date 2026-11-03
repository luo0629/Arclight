"""Public read models for quoted, confirmed and unresolved production costs."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_serializer, field_validator

CostDimension = Literal["project", "episode", "task", "model"]


class CostAmounts(BaseModel):
    estimated_cost: Decimal | None
    actual_cost: Decimal | None
    pending_estimate: Decimal

    @field_serializer("estimated_cost", "actual_cost", "pending_estimate")
    def money(self, value: Decimal | None) -> str | None:
        return format(value, ".6f") if value is not None else None


class CostTotals(CostAmounts):
    entry_count: int
    confirmed_count: int
    estimated_count: int
    unpriced_count: int
    projected_cost: Decimal | None
    cost_is_estimate: bool

    @field_serializer("projected_cost")
    def projected_money(self, value: Decimal | None) -> str | None:
        return format(value, ".6f") if value is not None else None


class CostGroup(CostTotals):
    key: str
    label: str
    episode_id: UUID | None = None
    task_id: UUID | None = None
    provider: str | None = None
    model: str | None = None


class CostReport(BaseModel):
    project_id: UUID
    currency: Literal["USD"] = "USD"
    budget_limit: Decimal
    generated_at: datetime
    group_by: CostDimension
    summary: CostTotals
    groups: list[CostGroup]
    total_groups: int
    limit: int
    offset: int

    @field_serializer("budget_limit")
    def budget_money(self, value: Decimal) -> str:
        return format(value, ".6f")


class CostEntry(CostAmounts):
    id: UUID
    source: Literal["MEDIA", "TEXT"]
    task_id: UUID
    task_type: str
    episode_id: UUID | None
    provider: str
    model: str
    attempt_id: UUID | None
    attempt_no: int
    call_id: UUID | None
    input_manifest_id: UUID | None
    trace_id: str | None
    status: str
    billing_source: str | None
    created_at: datetime
    cost_is_estimate: bool

    @field_validator("created_at")
    @classmethod
    def utc_date(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CostEntries(BaseModel):
    currency: Literal["USD"] = "USD"
    items: list[CostEntry]
    total: int
    limit: int
    offset: int
