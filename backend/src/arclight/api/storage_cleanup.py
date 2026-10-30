"""Owner-reviewed cleanup: bounded scan, observation period, one item per execution."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.storage_cleanup import execute, scan, utc
from arclight.core.errors import DomainError
from arclight.persistence.models import CleanupItem, CleanupPlan

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/storage-cleanups", tags=["storage-cleanup"]
)


class ScanInput(Input):
    namespace: Literal["TEMPORARY", "VALIDATED", "ASSET", "INTERMEDIATE"]
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    previous_plan_id: UUID | None = None


class ItemRead(Resource):
    byte_size: int
    reason: str
    status: str
    attempt_count: int
    error_code: str | None
    completed_at: datetime | None

    @field_validator("created_at", "updated_at", "completed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value is not None else None


def read_plan(db: Database, plan: CleanupPlan) -> dict:
    return {
        "id": plan.id,
        "namespace": plan.namespace,
        "scanned_at": utc(plan.scanned_at),
        "execute_after": utc(plan.execute_after),
        "expires_at": utc(plan.expires_at),
        "scanned_count": plan.scanned_count,
        "retained_count": plan.retained_count,
        "has_more": plan.next_cursor is not None,
        "items": [
            ItemRead.model_validate(row)
            for row in db.scalars(
                select(CleanupItem).where(CleanupItem.plan_id == plan.id).order_by(CleanupItem.id)
            )
        ],
    }


def find_plan(db: Database, project_id: UUID, plan_id: UUID) -> CleanupPlan:
    plan = db.get(CleanupPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        raise DomainError("NOT_FOUND", "清理计划不存在", 404)
    return plan


@router.post("", status_code=201)
def scan_cleanup(
    project_id: UUID,
    payload: ScanInput,
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> dict:
    require_project(db, project_id, actor, write=True, owner=True)
    previous = (
        find_plan(db, project_id, payload.previous_plan_id) if payload.previous_plan_id else None
    )
    plan = scan(
        db,
        request.app.state.settings,
        project_id,
        actor,
        payload.namespace,
        payload.limit,
        previous,
    )
    audit(
        db,
        project_id,
        actor,
        "storage.cleanup_planned",
        plan.id,
        {"namespace": plan.namespace, "scanned_count": plan.scanned_count},
    )
    response.headers["Cache-Control"] = "no-store"
    return read_plan(db, plan)


@router.get("")
def list_plans(
    project_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[dict]:
    require_project(db, project_id, actor, owner=True)
    response.headers["Cache-Control"] = "no-store"
    return [
        read_plan(db, plan)
        for plan in db.scalars(
            select(CleanupPlan)
            .where(CleanupPlan.project_id == project_id)
            .order_by(CleanupPlan.created_at.desc(), CleanupPlan.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/{plan_id}")
def get_plan(
    project_id: UUID, plan_id: UUID, db: Database, actor: Actor, response: Response
) -> dict:
    require_project(db, project_id, actor, owner=True)
    response.headers["Cache-Control"] = "no-store"
    return read_plan(db, find_plan(db, project_id, plan_id))


@router.post("/{plan_id}/items/{item_id}/execute")
def execute_item(
    project_id: UUID,
    plan_id: UUID,
    item_id: UUID,
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> ItemRead:
    require_project(db, project_id, actor, write=True, owner=True)
    plan = find_plan(db, project_id, plan_id)
    item = db.get(CleanupItem, item_id)
    if item is None or item.plan_id != plan.id:
        raise DomainError("NOT_FOUND", "清理目标不存在", 404)
    old_attempts = item.attempt_count
    execute(db, request.app.state.settings, plan, item, actor)
    if item.attempt_count != old_attempts:
        audit(
            db,
            project_id,
            actor,
            "storage.cleanup_attempted",
            item.id,
            {"plan_id": str(plan.id), "status": item.status, "error_code": item.error_code},
        )
    response.headers["Cache-Control"] = "no-store"
    return ItemRead.model_validate(item)
