"""Manifest render commands and durable, permission-checked execution history."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import and_, or_, select

from arclight.api.dependencies import Actor, Database
from arclight.application.render_job_store import read_attempts, read_job
from arclight.application.render_jobs import cancel_job, create_job
from arclight.application.render_manifests import manifest_row
from arclight.core.errors import DomainError
from arclight.domain.render_jobs import (
    RenderAttemptRead,
    RenderCancel,
    RenderJobCreate,
    RenderJobRead,
)
from arclight.persistence.models import RenderExecution, RenderJob

router = APIRouter(prefix="/api/v1", tags=["render-jobs"])


@router.post("/render-manifests/{manifest_id}/jobs", status_code=202)
def create(
    manifest_id: UUID,
    payload: RenderJobCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> RenderJobRead:
    response.headers["Cache-Control"] = "no-store"
    return create_job(db, request.app.state.settings, manifest_id, actor, payload)


@router.get("/render-manifests/{manifest_id}/jobs")
def history(
    manifest_id: UUID,
    response: Response,
    db: Database,
    actor: Actor,
    before_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[RenderJobRead]:
    response.headers["Cache-Control"] = "no-store"
    manifest_row(db, manifest_id, actor)
    query = (
        select(RenderExecution)
        .join(RenderJob, RenderJob.id == RenderExecution.id)
        .where(RenderJob.manifest_id == manifest_id)
    )
    if before_id:
        cursor, job = db.get(RenderExecution, before_id), db.get(RenderJob, before_id)
        if cursor is None or job is None or job.manifest_id != manifest_id:
            raise DomainError("NOT_FOUND", "本清单的渲染游标不存在", 404)
        query = query.where(
            or_(
                RenderExecution.created_at < cursor.created_at,
                and_(
                    RenderExecution.created_at == cursor.created_at, RenderExecution.id < cursor.id
                ),
            )
        )
    rows = db.scalars(
        query.order_by(RenderExecution.created_at.desc(), RenderExecution.id.desc()).limit(limit)
    )
    return [read_job(db, row.id, actor) for row in rows]


@router.get("/render-jobs/{run_id}")
def get(run_id: UUID, response: Response, db: Database, actor: Actor) -> RenderJobRead:
    response.headers["Cache-Control"] = "no-store"
    return read_job(db, run_id, actor)


@router.get("/render-jobs/{run_id}/attempts")
def attempts(
    run_id: UUID, response: Response, db: Database, actor: Actor
) -> list[RenderAttemptRead]:
    response.headers["Cache-Control"] = "no-store"
    return read_attempts(db, run_id, actor)


@router.post("/render-jobs/{run_id}/cancel")
def cancel(
    run_id: UUID, payload: RenderCancel, response: Response, db: Database, actor: Actor
) -> RenderJobRead:
    response.headers["Cache-Control"] = "no-store"
    return cancel_job(db, run_id, actor, payload)
