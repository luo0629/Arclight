"""Permission-scoped public media states and explicit cancellation intent."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.generation_state import find_job, request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation import STATUS_LABELS, GenerationStatus
from arclight.persistence.models import GenerationJob

router = APIRouter(prefix="/api/v1/projects/{project_id}/generation-jobs", tags=["generation"])


class GenerationRead(Resource):
    project_id: UUID
    episode_id: UUID | None
    target_type: str
    target_asset_id: UUID | None
    input_manifest_id: UUID
    task_type: str
    provider: str
    model: str
    status: GenerationStatus
    revision: int
    attempt_count: int
    cancel_requested_at: datetime | None
    status_label: str = ""

    @field_validator("created_at", "updated_at", "cancel_requested_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class GenerationCancel(Input):
    expected_revision: int = Field(strict=True, ge=0)


def read_job(row: GenerationJob) -> GenerationRead:
    result = GenerationRead.model_validate(row)
    result.status_label = STATUS_LABELS[result.status]
    return result


@router.get("")
def list_jobs(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[GenerationRead]:
    require_project(db, project_id, actor)
    rows = db.scalars(
        select(GenerationJob)
        .where(GenerationJob.project_id == project_id)
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id)
        .limit(limit)
        .offset(offset)
    )
    return [read_job(row) for row in rows]


@router.get("/{job_id}")
def get_job(project_id: UUID, job_id: UUID, db: Database, actor: Actor) -> GenerationRead:
    require_project(db, project_id, actor)
    row = find_job(db, job_id)
    if row.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    return read_job(row)


@router.post("/{job_id}/cancel")
def cancel_job(
    project_id: UUID,
    job_id: UUID,
    payload: GenerationCancel,
    db: Database,
    actor: Actor,
) -> GenerationRead:
    require_project(db, project_id, actor, write=True)
    row = find_job(db, job_id)
    if row.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    return read_job(request_cancel(db, job_id, payload.expected_revision, actor))
