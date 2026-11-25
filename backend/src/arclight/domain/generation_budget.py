"""Server-owned money contracts; all amounts share the explicit USD accounting unit."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from arclight.domain.generation_requests import MediaInput, safe_metadata

Money = Annotated[Decimal, Field(ge=0, lt=Decimal("10000000000"), decimal_places=6)]
ProjectMoney = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class CostQuote(MediaInput):
    currency: Literal["USD"] = "USD"
    maximum_cost: Money
    pricing_version: str = Field(min_length=1, max_length=100)


class BudgetSettlementInput(MediaInput):
    cost: Annotated[Decimal, Field(ge=0, lt=Decimal("1000000000000"), decimal_places=6)]
    confirmed: bool = Field(strict=True)
    evidence_reference: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")

    @model_validator(mode="after")
    def confirmed_evidence(self):
        if not self.confirmed or "://" in self.evidence_reference:
            raise ValueError("Final billing requires confirmed private evidence, not a URL")
        safe_metadata(self.evidence_reference)
        return self
