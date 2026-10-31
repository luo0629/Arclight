"""Server-controlled media limits and explicit failure/retry policy."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveLimit = Annotated[int, Field(strict=True, ge=1, le=100000)]
RetryError = Literal["PROVIDER_FAILED", "PROVIDER_UNAVAILABLE"]


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    provider_concurrency: PositiveLimit = 4
    provider_limits: dict[str, PositiveLimit] = Field(default_factory=dict)
    project_concurrency: PositiveLimit = 2
    project_limits: dict[str, PositiveLimit] = Field(default_factory=dict)
    paused_providers: frozenset[str] = frozenset()
    global_per_minute: PositiveLimit = 60
    user_per_minute: PositiveLimit = 20
    call_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    max_elapsed_seconds: float = Field(default=900, gt=0, le=86400)
    max_attempts: int = Field(default=3, strict=True, ge=1, le=20)
    retry_base_seconds: float = Field(default=2, gt=0, le=3600)
    retry_max_seconds: float = Field(default=60, gt=0, le=3600)
    retry_errors: frozenset[RetryError] = frozenset({"PROVIDER_FAILED", "PROVIDER_UNAVAILABLE"})
    automatic_retry_errors: frozenset[RetryError] = frozenset({"PROVIDER_UNAVAILABLE"})

    @model_validator(mode="after")
    def coherent_policy(self):
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("Retry base cannot exceed the maximum backoff")
        if not self.automatic_retry_errors <= self.retry_errors:
            raise ValueError("Automatic retry errors must be inside the retry allowlist")
        if len(self.provider_limits) > 100 or len(self.project_limits) > 10000:
            raise ValueError("Too many scoped concurrency overrides")
        return self

    def backoff(self, attempt_count: int) -> float:
        return min(
            self.retry_max_seconds, self.retry_base_seconds * 2 ** min(attempt_count - 1, 20)
        )


DEFAULT_EXECUTION_POLICY = ExecutionPolicy()

ERROR_CATEGORIES = {
    "INPUT_INVALID": "INPUT",
    "PROVIDER_REJECTED": "SERVICE",
    "PROVIDER_FAILED": "SERVICE",
    "PROVIDER_UNAVAILABLE": "SERVICE",
    "RESULT_UNKNOWN": "UNCERTAIN",
    "STORAGE_FAILED": "STORAGE",
    "CANCELLED": "CANCELLATION",
    "BUDGET_EXCEEDED": "BUDGET",
    "QUALITY_FAILED": "QUALITY",
}
