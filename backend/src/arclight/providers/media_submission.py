"""Normalized, bounded media submissions selected from installed adapters."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from arclight.core.config import Settings
from arclight.domain.generation_requests import GenerationResult, MediaErrorCode, MediaInput
from arclight.domain.media_capabilities import MediaCapabilities
from arclight.providers.media_capabilities import MOCK_CAPABILITIES


@dataclass(frozen=True)
class MediaSubmission:
    job_id: UUID
    attempt_id: UUID
    input_manifest_id: UUID
    snapshot: dict
    idempotency_key: str | None


class SubmissionReceipt(MediaInput):
    status: Literal["SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "RESULT_UNKNOWN"]
    request_id: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    result: GenerationResult | None = None
    error_code: MediaErrorCode | None = None
    latency_ms: int | None = Field(default=None, strict=True, ge=0, le=2147483647)
    cost: Decimal | None = Field(default=None, ge=0, lt=Decimal("1000000000000"), decimal_places=6)

    @model_validator(mode="after")
    def consistent_receipt(self):
        if (self.status == "SUCCEEDED") != (self.result is not None):
            raise ValueError("Success requires the normalized result")
        if self.status in {"FAILED", "CANCELLED"}:
            if self.error_code is None or self.error_code == "RESULT_UNKNOWN":
                raise ValueError("Definite failure/cancellation requires a definite error")
        elif self.status == "RESULT_UNKNOWN":
            if (
                self.error_code != "RESULT_UNKNOWN"
                or self.cost is not None
                or self.latency_ms is not None
            ):
                raise ValueError("Unknown execution cannot invent a definite result or usage")
        elif self.error_code is not None:
            raise ValueError("Accepted/successful execution cannot carry a failure")
        if self.status in {"SUBMITTED", "RUNNING"} and (
            self.request_id is None or self.cost is not None or self.latency_ms is not None
        ):
            raise ValueError("Accepted jobs require a query identity; usage is final-only")
        return self


class SubmissionUncertain(Exception):
    """Transport failed without authoritative evidence of non-acceptance."""


class MediaSubmissionProvider(Protocol):
    name: str
    supports_idempotency: bool
    capabilities: MediaCapabilities

    def submit(self, request: MediaSubmission) -> SubmissionReceipt: ...


class MockMediaSubmissionProvider:
    name = "mock"
    supports_idempotency = True
    capabilities = MOCK_CAPABILITIES

    def submit(self, request: MediaSubmission) -> SubmissionReceipt:
        # A normalized Mock receipt, not a registered/approved file or current version.
        return SubmissionReceipt(
            status="SUCCEEDED",
            request_id=f"mock:{request.attempt_id}",
            result=GenerationResult(
                output_handles=[
                    f"mock/image/{request.attempt_id}/{index}"
                    for index in range(
                        request.snapshot["context"]["generation"].get("batch_size", 1)
                    )
                ]
            ),
        )


def get_media_submission_provider(
    provider: str = "mock", settings: Settings | None = None
) -> MediaSubmissionProvider:
    if provider == "openai" and settings is not None:
        from arclight.providers.openai_images import OpenAIImageProvider

        return OpenAIImageProvider(settings)
    if provider != "mock":
        from arclight.core.errors import DomainError

        raise DomainError("PROVIDER_NOT_INSTALLED", "图像供应商未安装或缺少服务器配置", 422)
    return MockMediaSubmissionProvider()
