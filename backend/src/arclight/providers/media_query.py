"""Read-only media reconciliation contract; querying never submits generation."""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field, model_validator

from arclight.domain.generation_requests import GenerationResult, MediaErrorCode, MediaInput
from arclight.domain.media_capabilities import MediaCapabilities
from arclight.providers.media_capabilities import MOCK_CAPABILITIES

if TYPE_CHECKING:
    from arclight.providers.media_submission import MediaSubmission


@dataclass(frozen=True)
class ProviderLookup:
    provider: str
    model: str
    request_id: str
    submission: "MediaSubmission | None" = None


class QueryReceipt(MediaInput):
    status: Literal[
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "NOT_ACCEPTED",
        "UNKNOWN",
        "NOT_FOUND",
        "UNSUPPORTED",
        "QUERY_FAILED",
        "INVALID_RESPONSE",
    ]
    result: GenerationResult | None = None
    error_code: MediaErrorCode | None = None
    latency_ms: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    cost: Decimal | None = Field(default=None, ge=0, lt=Decimal("1000000000000"), decimal_places=6)

    @model_validator(mode="after")
    def definite_result(self):
        if (self.status == "SUCCEEDED") != (self.result is not None):
            raise ValueError("Only confirmed success can contain a normalized result")
        if self.error_code is not None and (
            self.status != "FAILED" or self.error_code in {"RESULT_UNKNOWN", "CANCELLED"}
        ):
            raise ValueError("Only definite failure can contain a definite failure code")
        if self.status in {
            "UNKNOWN",
            "NOT_FOUND",
            "UNSUPPORTED",
            "QUERY_FAILED",
            "INVALID_RESPONSE",
            "NOT_ACCEPTED",
        } and (self.cost is not None or self.latency_ms is not None):
            raise ValueError("Unconfirmed execution cannot invent usage")
        return self


class ProviderQueryError(Exception):
    """Adapter query failed. Raw transport errors must never enter business history."""


class MediaQueryProvider(Protocol):
    capabilities: MediaCapabilities

    def query(self, lookup: ProviderLookup) -> QueryReceipt: ...


class MockMediaQueryProvider:
    capabilities = MOCK_CAPABILITIES

    def query(self, lookup: ProviderLookup) -> QueryReceipt:
        # Mock generation has no external durable query service.
        return QueryReceipt(status="UNSUPPORTED")


def get_media_query_provider() -> MediaQueryProvider:
    return MockMediaQueryProvider()
