"""Permission-scoped public media states and explicit cancellation intent."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.generation_attempts import request_retry
from arclight.application.generation_jobs import create_job
from arclight.application.generation_reconciliation import ManualResolution, reconcile
from arclight.application.generation_replay import ReplayRequest, replay_job
from arclight.application.generation_state import find_job, request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation import STATUS_LABELS, GenerationStatus
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.generation_scheduling import ERROR_CATEGORIES
from arclight.domain.media_capabilities import MediaCapabilities
from arclight.persistence.generation import (
    GenerationDispatch,
    GenerationLease,
    GenerationReconciliation,
)
from arclight.persistence.generation_queues import (
    GenerationLane,
    GenerationPermit,
    GenerationPermitEvent,
)
from arclight.persistence.generation_scheduling import (
    GenerationCall,
    GenerationDeadLetter,
    GenerationReplay,
    GenerationSchedule,
)
from arclight.persistence.models import GenerationAttempt, GenerationJob
from arclight.providers.media_capabilities import resolve_capabilities
from arclight.providers.media_query import MediaQueryProvider, get_media_query_provider

router = APIRouter(prefix="/api/v1/projects/{project_id}/generation-jobs", tags=["generation"])


def utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
        return utc_timestamp(value)


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
    error_category: str | None = None

    @classmethod
    def from_attempt(cls, row: GenerationAttempt) -> "AttemptRead":
        result = cls.model_validate(row)
        result.error_category = ERROR_CATEGORIES.get(row.error_code or "")
        return result

    @field_validator("created_at", "updated_at", "submitted_at", "finished_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value)


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
    request: Request,
    db: Database,
    actor: Actor,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160, pattern=r"^[!-~]+$")],
) -> GenerationRead:
    return read_job(
        create_job(
            db,
            project_id,
            actor,
            idempotency_key,
            payload,
            execution_policy=request.app.state.settings.media_execution_policy,
        )
    )


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


@router.get("/providers/{provider}/capabilities")
def get_capabilities(
    project_id: UUID,
    provider: str,
    db: Database,
    actor: Actor,
    model: Annotated[str, Query(min_length=1, max_length=160)] = "mock-image-v1",
) -> MediaCapabilities:
    require_project(db, project_id, actor)
    return resolve_capabilities(provider, model)


@router.get("/queue")
def get_queue(project_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    from arclight.application.generation_queue_reads import queue_overview

    return queue_overview(db, project_id)


class QueueResource(Resource):
    @field_validator("created_at", "updated_at")
    @classmethod
    def queue_timestamps(cls, value: datetime) -> datetime | None:
        return utc_timestamp(value)


class LaneRead(QueueResource):
    job_id: UUID
    lane: str
    origin: str
    policy_version: int
    actor: str


class PermitRead(QueueResource):
    dispatch_id: UUID
    job_id: UUID
    lane: str
    state: str
    epoch: int
    issued_at: datetime | None
    expires_at: datetime | None
    publish_after: datetime | None
    publish_count: int
    reason: str | None

    @field_validator("issued_at", "expires_at", "publish_after")
    @classmethod
    def permit_timestamps(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value)


class PermitEventRead(QueueResource):
    permit_id: UUID
    sequence: int
    epoch: int
    action: str
    reason: str | None


@router.get("/{job_id}/queue")
def get_job_queue(project_id: UUID, job_id: UUID, db: Database, actor: Actor) -> LaneRead:
    scoped_job(db, project_id, job_id, actor)
    return LaneRead.model_validate(
        db.scalar(select(GenerationLane).where(GenerationLane.job_id == job_id))
    )


@router.get("/{job_id}/permits")
def list_permits(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[PermitRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationPermit)
        .where(GenerationPermit.job_id == job_id)
        .order_by(GenerationPermit.created_at, GenerationPermit.id)
        .limit(limit)
        .offset(offset)
    )
    return [PermitRead.model_validate(row) for row in rows]


@router.get("/{job_id}/permits/{permit_id}/events")
def list_permit_events(
    project_id: UUID,
    job_id: UUID,
    permit_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[PermitEventRead]:
    scoped_job(db, project_id, job_id, actor)
    permit = db.get(GenerationPermit, permit_id)
    if permit is None or permit.job_id != job_id:
        raise DomainError("NOT_FOUND", "调度许可不存在", 404)
    rows = db.scalars(
        select(GenerationPermitEvent)
        .where(GenerationPermitEvent.permit_id == permit_id)
        .order_by(GenerationPermitEvent.sequence)
        .limit(limit)
        .offset(offset)
    )
    return [PermitEventRead.model_validate(row) for row in rows]


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
    return [AttemptRead.from_attempt(row) for row in rows]


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
    return AttemptRead.from_attempt(row)


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
        return utc_timestamp(value)


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
        return utc_timestamp(value)


class ExecutionRead(Resource):
    job_id: UUID
    attempt_id: UUID
    epoch: int
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None

    @field_validator("created_at", "updated_at", "heartbeat_at", "expires_at", "released_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value)


@router.get("/{job_id}/execution")
def get_execution(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
) -> ExecutionRead | None:
    scoped_job(db, project_id, job_id, actor)
    row = db.scalar(select(GenerationLease).where(GenerationLease.job_id == job_id))
    return ExecutionRead.model_validate(row) if row is not None else None


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
            execution_policy=request.app.state.settings.media_execution_policy,
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
            execution_policy=request.app.state.settings.media_execution_policy,
        )
    )


class SchedulingRead(Resource):
    @field_validator("*", mode="after")
    @classmethod
    def utc_values(cls, value):
        if isinstance(value, datetime):
            return utc_timestamp(value)
        return value


class ScheduleRead(SchedulingRead):
    job_id: UUID
    policy_json: dict
    deadline_at: datetime
    next_attempt_at: datetime
    blocked_code: str | None


class DeadLetterRead(SchedulingRead):
    job_id: UUID
    attempt_id: UUID | None
    reason: str
    error_code: str | None
    category: str
    diagnostic_json: dict


class ReplayRead(SchedulingRead):
    project_id: UUID
    source_job_id: UUID
    dead_letter_id: UUID
    new_job_id: UUID
    actor: str
    reason: str


class CallRead(SchedulingRead):
    job_id: UUID
    attempt_id: UUID
    kind: str
    started_at: datetime
    completed_at: datetime | None
    released_at: datetime | None
    outcome: str | None


@router.get("/{job_id}/schedule")
def get_schedule(project_id: UUID, job_id: UUID, db: Database, actor: Actor) -> ScheduleRead | None:
    scoped_job(db, project_id, job_id, actor)
    row = db.scalar(select(GenerationSchedule).where(GenerationSchedule.job_id == job_id))
    return ScheduleRead.model_validate(row) if row else None


@router.get("/{job_id}/dead-letter")
def get_dead_letter(
    project_id: UUID, job_id: UUID, db: Database, actor: Actor
) -> DeadLetterRead | None:
    scoped_job(db, project_id, job_id, actor)
    row = db.scalar(select(GenerationDeadLetter).where(GenerationDeadLetter.job_id == job_id))
    return DeadLetterRead.model_validate(row) if row else None


@router.get("/{job_id}/calls")
def list_calls(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[CallRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationCall)
        .where(GenerationCall.job_id == job_id)
        .order_by(GenerationCall.started_at, GenerationCall.id)
        .limit(limit)
        .offset(offset)
    )
    return [CallRead.model_validate(row) for row in rows]


@router.get("/{job_id}/replays")
def list_replays(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[ReplayRead]:
    scoped_job(db, project_id, job_id, actor)
    rows = db.scalars(
        select(GenerationReplay)
        .where(
            GenerationReplay.project_id == project_id,
            (GenerationReplay.source_job_id == job_id) | (GenerationReplay.new_job_id == job_id),
        )
        .order_by(GenerationReplay.created_at, GenerationReplay.id)
        .limit(limit)
        .offset(offset)
    )
    return [ReplayRead.model_validate(row) for row in rows]


@router.post("/{job_id}/replay", status_code=201)
def replay_generation(
    project_id: UUID,
    job_id: UUID,
    payload: ReplayRequest,
    request: Request,
    db: Database,
    actor: Actor,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160, pattern=r"^[!-~]+$")],
) -> GenerationRead:
    return read_job(
        replay_job(
            db,
            project_id,
            job_id,
            actor,
            idempotency_key,
            payload,
            request.app.state.settings.media_execution_policy,
        )
    )
