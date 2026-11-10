"""Public renderer controls expose safe state, never process tokens or storage paths."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arclight.domain.render_inspections import RenderInspectionResult


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RenderJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    creation_key: UUID
    retry_of: UUID | None = None


class RenderCancel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(strict=True, ge=1)


class RenderJobRead(BaseModel):
    id: UUID
    manifest_id: UUID
    manifest_sha256: str
    retry_of_id: UUID | None
    profile: Literal["PREVIEW", "FORMAL"]
    status: str
    revision: int
    progress: int
    phase: str
    attempt_count: int
    cancel_requested_at: datetime | None
    output_file_id: UUID | None
    output_asset_version_id: UUID | None = None
    artifact_id: UUID | None = None
    error_code: str | None
    cleanup_status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    is_delivery: Literal[False] = False

    @field_validator("cancel_requested_at", "created_at", "started_at", "finished_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class RenderAttemptRead(BaseModel):
    attempt_no: int
    status: str
    progress: int
    phase: str
    error_code: str | None
    cleanup_status: str
    cleanup_error_code: str | None
    created_at: datetime
    finished_at: datetime | None
    inspection: RenderInspectionResult | None = None

    @field_validator("created_at", "finished_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value)
