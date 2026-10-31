"""Server-owned money contracts; all amounts share the explicit USD accounting unit."""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.generation_requests import MediaInput, safe_metadata

Money = Annotated[Decimal, Field(ge=0, lt=Decimal("10000000000"), decimal_places=6)]
ProjectMoney = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class CostQuote(MediaInput):
    currency: Literal["USD"] = "USD"
    maximum_cost: Money
    pricing_version: str = Field(min_length=1, max_length=100)


class BudgetPolicyInput(MediaInput):
    expected_revision: int = Field(strict=True, ge=0)
    project_limit: ProjectMoney
    task_limit: Money | None = None
    episode_limit: Money | None = None
    batch_limit: Money | None = None
    warning_percent: int = Field(default=80, strict=True, ge=1, le=100)
    max_candidates_per_target: int = Field(default=64, strict=True, ge=1, le=10000)


class BudgetBatchInput(MediaInput):
    creation_key: UUID
    episode_id: UUID | None = None
    budget_limit: Money


class BudgetAdjustmentInput(MediaInput):
    creation_key: UUID
    requested_limit: ProjectMoney
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if not self.reason.strip():
            raise ValueError("A budget adjustment requires a reason")
        safe_metadata(self.reason)
        return self


class BudgetDecisionInput(MediaInput):
    expected_revision: int = Field(strict=True, ge=0)
    approved: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if not self.reason.strip():
            raise ValueError("A budget decision requires a reason")
        safe_metadata(self.reason)
        return self
