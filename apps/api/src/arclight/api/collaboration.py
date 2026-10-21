from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field, model_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import EpisodeRead, Input, ProjectRead, RunRead
from arclight.application.commands import require_episode, require_project
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    Project,
    ProjectMember,
    User,
    WorkflowRun,
)

router = APIRouter(prefix="/api/v1", tags=["collaboration"])


class ProjectPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    style_guide: dict | None = None
    output_profile: dict | None = None
    budget_limit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    archived: bool | None = None

    @model_validator(mode="after")
    def reject_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


class EpisodePatch(Input):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    target_duration_ms: int | None = Field(default=None, ge=2500, le=1800000)
    archived: bool | None = None

    @model_validator(mode="after")
    def reject_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


class MemberInput(Input):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["editor", "viewer"]


class MemberRole(Input):
    role: Literal["editor", "viewer"]


def audit(db, project_id: UUID, actor: str, action: str, resource_id: UUID, details=None):
    db.add(
        AuditEvent(
            project_id=project_id,
            actor_id=actor,
            action=action,
            resource_id=resource_id,
            details_json=details or {},
        )
    )


def require_idle(db, episode_ids):
    if db.scalar(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.episode_id.in_(episode_ids),
            WorkflowRun.status.in_(["QUEUED", "RUNNING", "WAITING_REVIEW"]),
        )
        .limit(1)
    ):
        raise DomainError("ACTIVE_WORKFLOW", "请先结束或取消正在进行的工作流", 409)


@router.get("/projects/{project_id}/access")
def access(project_id: UUID, db: Database, actor: Actor) -> dict:
    project = require_project(db, project_id, actor)
    role = (
        "owner"
        if project.owner_id == actor
        else db.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == UUID(actor)
            )
        )
    )
    return {"role": role, "archived": project.archived}


@router.patch("/projects/{project_id}")
def patch_project(
    project_id: UUID, payload: ProjectPatch, db: Database, actor: Actor
) -> ProjectRead:
    # 归档后的解档操作仍需允许 Owner 执行；其他修改必须遵循只读约束。
    project = require_project(db, project_id, actor, owner=True)
    db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    changes = payload.model_dump(exclude_unset=True)
    if project.archived and changes != {"archived": False}:
        raise DomainError("PROJECT_ARCHIVED", "请先恢复项目再编辑", 409)
    if changes:
        require_idle(db, select(Episode.id).where(Episode.project_id == project_id))
    for key, value in changes.items():
        setattr(project, key, value)
    audit(db, project_id, actor, "project.updated", project_id, {"fields": list(changes)})
    db.flush()
    return ProjectRead.model_validate(project)


@router.patch("/episodes/{episode_id}")
def patch_episode(
    episode_id: UUID, payload: EpisodePatch, db: Database, actor: Actor
) -> EpisodeRead:
    episode = require_episode(db, episode_id, actor)
    changes = payload.model_dump(exclude_unset=True)
    require_project(db, episode.project_id, actor, write=True, owner="archived" in changes)
    db.execute(select(Episode.id).where(Episode.id == episode_id).with_for_update())
    if episode.status == "ARCHIVED" and changes != {"archived": False}:
        raise DomainError("EPISODE_ARCHIVED", "请先恢复剧集再编辑", 409)
    require_idle(db, [episode_id])
    for key, value in changes.items():
        if key == "archived":
            episode.status = "ARCHIVED" if value else "DRAFT"
        else:
            setattr(episode, key, value)
    audit(db, episode.project_id, actor, "episode.updated", episode_id, {"fields": list(changes)})
    db.flush()
    return EpisodeRead.model_validate(episode)


@router.get("/projects/{project_id}/members")
def members(project_id: UUID, db: Database, actor: Actor) -> list[dict]:
    require_project(db, project_id, actor)
    rows = db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.display_name)
    )
    return [
        {
            "user_id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "role": member.role,
        }
        for member, user in rows
    ]


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(project_id: UUID, payload: MemberInput, db: Database, actor: Actor) -> dict:
    project = require_project(db, project_id, actor, write=True, owner=True)
    user = db.scalar(
        select(User).where(User.email == payload.email.lower(), User.disabled.is_(False))
    )
    if user is None:
        raise DomainError("USER_NOT_FOUND", "请该成员先注册账号", 404)
    if str(user.id) == project.owner_id:
        raise DomainError("OWNER_MEMBERSHIP", "Owner 不需要重复添加为成员", 409)
    member = ProjectMember(project_id=project_id, user_id=user.id, role=payload.role)
    db.add(member)
    audit(db, project_id, actor, "member.added", user.id, {"role": payload.role})
    db.flush()
    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": member.role,
    }


@router.patch("/projects/{project_id}/members/{user_id}")
def change_member(
    project_id: UUID, user_id: UUID, payload: MemberRole, db: Database, actor: Actor
) -> dict:
    require_project(db, project_id, actor, write=True, owner=True)
    member = db.scalar(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .with_for_update()
    )
    if member is None:
        raise DomainError("NOT_FOUND", "项目成员不存在", 404)
    member.role = payload.role
    audit(db, project_id, actor, "member.role_changed", user_id, {"role": payload.role})
    return {"user_id": str(user_id), "role": member.role}


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(project_id: UUID, user_id: UUID, db: Database, actor: Actor):
    require_project(db, project_id, actor, write=True, owner=True)
    member = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if member is None:
        raise DomainError("NOT_FOUND", "项目成员不存在", 404)
    db.delete(member)
    audit(db, project_id, actor, "member.removed", user_id)


@router.get("/projects/{project_id}/audit-events")
def audits(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 50, offset: Offset = 0
) -> list[dict]:
    require_project(db, project_id, actor, owner=True)
    return [
        {
            "id": str(row.id),
            "actor_id": row.actor_id,
            "action": row.action,
            "resource_id": str(row.resource_id),
            "details": row.details_json,
            "created_at": row.created_at.isoformat(),
        }
        for row in db.scalars(
            select(AuditEvent)
            .where(AuditEvent.project_id == project_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/episodes/{episode_id}/runs")
def runs(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[RunRead]:
    require_episode(db, episode_id, actor)
    return [
        RunRead.model_validate(row)
        for row in db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.episode_id == episode_id)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
            .limit(limit)
            .offset(offset)
        )
    ]
