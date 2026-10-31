"""Trusted adapter pricing boundary. Client parameters never supply a price."""

from decimal import Decimal

from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationSpec


def quote(spec: GenerationSpec, task_type: str) -> CostQuote | None:
    # Real adapters must supply a bounded quote before they can become billable.
    if spec.provider == "mock":
        return CostQuote(maximum_cost=Decimal("0"), pricing_version="mock-free-v1")
    return None
