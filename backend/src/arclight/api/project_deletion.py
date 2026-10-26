"""Owner-only logical deletion; never physical erasure of production dependencies."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select

from arclight.api.collaboration import audit, require_idle
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, ProjectRead
from arclight.core.errors import DomainError
from arclight.persistence.base import utcnow
from arclight.persistence.models import Episode, Project, ProjectMember

router = APIRouter(prefix="/api/v1", tags=["project-deletion"])
RESTORE_DAYS = 30


class DeleteInput(Input):
    confirm_name: str = Field(min_length=1, max_length=160)


class RestoreInput(Input):
    deletion_id: UUID


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def owner_project(db, project_id: UUID, actor: str) -> Project:
    project = db.get(Project, project_id, populate_existing=True, with_for_update=True)
    if project is None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
    if project.owner_id != actor:
        member = (
            None
            if actor == "service-owner"
            else db.scalar(
                select(ProjectMember.id).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == UUID(actor)
                )
            )
        )
        if member is None or project.deleted_at is not None:
            raise DomainError("NOT_FOUND", "项目不存在", 404)
        raise DomainError("FORBIDDEN", "只有项目所有者可以删除或恢复项目", 403)
    return project


def receipt(project: Project) -> dict:
    assert project.deleted_at is not None and project.restore_until is not None
    return {
        "id": str(project.id),
        "name": project.name,
        "deletion_id": str(project.deletion_id),
        "deleted_at": as_utc(project.deleted_at).isoformat(),
        "restore_until": as_utc(project.restore_until).isoformat(),
        "can_restore": utcnow() < as_utc(project.restore_until),
    }


@router.get("/project-trash")
def trash(db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0) -> list[dict]:
    return [
        receipt(project)
        for project in db.scalars(
            select(Project)
            .where(Project.owner_id == actor, Project.deleted_at.is_not(None))
            .order_by(Project.deleted_at.desc(), Project.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.delete("/projects/{project_id}")
def delete_project(project_id: UUID, payload: DeleteInput, db: Database, actor: Actor) -> dict:
    project = owner_project(db, project_id, actor)
    if payload.confirm_name != project.name:
        raise DomainError("CONFIRMATION_MISMATCH", "请输入完整项目名称确认删除", 409)
    if project.deleted_at is not None:
        return receipt(project)
    if not project.archived:
        raise DomainError("ARCHIVE_REQUIRED", "请先归档项目，再申请删除", 409)
    require_idle(db, select(Episode.id).where(Episode.project_id == project_id))
    project.deleted_at = utcnow()
    project.restore_until = project.deleted_at + timedelta(days=RESTORE_DAYS)
    project.deletion_id = uuid4()
    audit(
        db,
        project_id,
        actor,
        "project.deleted",
        project_id,
        {
            "deletion_id": str(project.deletion_id),
            "restore_until": project.restore_until.isoformat(),
            "policy": "logical-delete-v1-30d-no-purge",
        },
    )
    db.flush()
    return receipt(project)


@router.post("/projects/{project_id}/restore")
def restore_project(
    project_id: UUID, payload: RestoreInput, db: Database, actor: Actor
) -> ProjectRead:
    project = owner_project(db, project_id, actor)
    if project.deleted_at is None or project.deletion_id != payload.deletion_id:
        raise DomainError("DELETION_CONFLICT", "删除状态已变化，请刷新回收站", 409)
    assert project.restore_until is not None
    if utcnow() >= as_utc(project.restore_until):
        raise DomainError(
            "RESTORE_EXPIRED", "30 天恢复窗口已结束，请联系管理员；数据尚未物理清理", 409
        )
    audit(
        db,
        project_id,
        actor,
        "project.restored",
        project_id,
        {
            "deletion_id": str(project.deletion_id),
        },
    )
    project.deleted_at = None
    project.restore_until = None
    project.deletion_id = None
    # Restoring access does not implicitly unarchive or start production.
    project.archived = True
    db.flush()
    return ProjectRead.model_validate(project)
