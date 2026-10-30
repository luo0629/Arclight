"""Permission-scoped public media states and explicit cancellation intent."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.generation_attempts import request_retry
from arclight.application.generation_jobs import create_job
from arclight.application.generation_reconciliation import ManualResolution, reconcile
from arclight.application.generation_state import find_job, request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation import STATUS_LABELS, GenerationStatus
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.generation import GenerationDispatch, GenerationReconciliation
from arclight.persistence.models import GenerationAttempt, GenerationJob
from arclight.providers.media_query import MediaQueryProvider, get_media_query_provider

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
    provider_request_id: str | None
    expected_asset_revision: int | None
    expected_lock_version: int | None
    expected_current_version_id: UUID | None
    created_by: str
    status_label: str = ""

    @field_validator("created_at", "updated_at", "cancel_requested_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class GenerationCancel(Input):
    expected_revision: int = Field(strict=True, ge=0)


class AttemptRead(Resource):
    job_id: UUID
    attempt_no: int
    input_manifest_id: UUID
    request_fingerprint: str
    provider: str
    model: str
    provider_request_id: str | None
    status: GenerationStatus
    input_snapshot_json: dict
    result_json: dict | None
    error_code: str | None
    latency_ms: int | None
    cost: Decimal | None
    submitted_at: datetime
    finished_at: datetime | None

    @field_validator("created_at", "updated_at", "submitted_at", "finished_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def read_job(row: GenerationJob) -> GenerationRead:
    result = GenerationRead.model_validate(row)
    result.status_label = STATUS_LABELS[result.status]
    return result


def scoped_job(
    db: Database, project_id: UUID, job_id: UUID, actor: str, *, write: bool = False
) -> GenerationJob:
    require_project(db, project_id, actor, write=write)
    row = find_job(db, job_id)
    if row.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    return row


@router.post("", status_code=201, summary="冻结输入并排队媒体任务（HTTP请求内不调用供应商）")
def capture_job(
    project_id: UUID,
    payload: GenerationCreate,
    db: Database,
    actor: Actor,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160, pattern=r"^[!-~]+$")],
) -> GenerationRead:
    return read_job(create_job(db, project_id, actor, idempotency_key, payload))


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
    return read_job(scoped_job(db, project_id, job_id, actor))


@router.get("/{job_id}/attempts")
def list_attempts(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[AttemptRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationAttempt)
        .where(
            GenerationAttempt.job_id == job_id,
        )
        .order_by(GenerationAttempt.attempt_no)
        .limit(limit)
        .offset(offset)
    )
    return [AttemptRead.model_validate(row) for row in rows]


@router.get("/{job_id}/attempts/{attempt_id}")
def get_attempt(
    project_id: UUID,
    job_id: UUID,
    attempt_id: UUID,
    db: Database,
    actor: Actor,
) -> AttemptRead:
    scoped_job(db, project_id, job_id, actor)
    row = db.get(GenerationAttempt, attempt_id)
    if row is None or row.job_id != job_id:
        raise DomainError("NOT_FOUND", "媒体尝试不存在", 404)
    return AttemptRead.model_validate(row)


@router.post("/{job_id}/retry")
def retry_job(
    project_id: UUID,
    job_id: UUID,
    payload: GenerationCancel,
    db: Database,
    actor: Actor,
) -> GenerationRead:
    scoped_job(db, project_id, job_id, actor, write=True)
    return read_job(request_retry(db, job_id, payload.expected_revision, actor))


@router.post("/{job_id}/cancel")
def cancel_job(
    project_id: UUID,
    job_id: UUID,
    payload: GenerationCancel,
    db: Database,
    actor: Actor,
) -> GenerationRead:
    scoped_job(db, project_id, job_id, actor, write=True)
    return read_job(request_cancel(db, job_id, payload.expected_revision, actor))


class ReconciliationRead(Resource):
    job_id: UUID
    attempt_id: UUID
    claimed_revision: int
    source: str
    actor: str
    evidence_reference: str | None
    receipt_json: dict | None
    disposition: str
    finished_at: datetime | None

    @field_validator("created_at", "updated_at", "finished_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class DispatchRead(Resource):
    job_id: UUID
    expected_revision: int
    request_fingerprint: str
    status: str
    attempt_id: UUID | None
    provider_supports_idempotency: bool | None
    provider_idempotency_key: str | None
    receipt_json: dict | None
    finished_at: datetime | None

    @field_validator("created_at", "updated_at", "finished_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


@router.get("/{job_id}/dispatches")
def list_dispatches(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[DispatchRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationDispatch)
        .where(GenerationDispatch.job_id == job_id)
        .order_by(GenerationDispatch.expected_revision)
        .limit(limit)
        .offset(offset)
    )
    return [DispatchRead.model_validate(row) for row in rows]


@router.get("/{job_id}/reconciliations")
def list_reconciliations(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[ReconciliationRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationReconciliation)
        .where(GenerationReconciliation.job_id == job_id)
        .order_by(GenerationReconciliation.claimed_revision)
        .limit(limit)
        .offset(offset)
    )
    return [ReconciliationRead.model_validate(row) for row in rows]


@router.post("/{job_id}/reconcile")
def reconcile_job(
    project_id: UUID,
    job_id: UUID,
    payload: GenerationCancel,
    request: Request,
    db: Database,
    actor: Actor,
    provider: Annotated[MediaQueryProvider, Depends(get_media_query_provider)],
) -> GenerationRead:
    scoped_job(db, project_id, job_id, actor, write=True)
    db.commit()  # End dependency/auth transaction before any provider query.
    return read_job(
        reconcile(
            request.app.state.session_factory,
            project_id,
            job_id,
            payload.expected_revision,
            actor,
            provider,
        )
    )


@router.post("/{job_id}/resolve")
def resolve_job(
    project_id: UUID,
    job_id: UUID,
    payload: ManualResolution,
    request: Request,
    db: Database,
    actor: Actor,
    provider: Annotated[MediaQueryProvider, Depends(get_media_query_provider)],
) -> GenerationRead:
    scoped_job(db, project_id, job_id, actor, write=True)
    db.commit()
    return read_job(
        reconcile(
            request.app.state.session_factory,
            project_id,
            job_id,
            payload.expected_revision,
            actor,
            provider,
            manual=payload,
        )
    )
