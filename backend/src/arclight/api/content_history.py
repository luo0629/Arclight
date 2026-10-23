"""只读内容对比快照：精确来源、可追溯作者与当前已实现的直接依赖。"""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_episode
from arclight.core.errors import DomainError
from arclight.persistence.models import AuditEvent, Script, Shot, Storyboard, User, WorkflowRun

router = APIRouter()


class Reference(BaseModel):
    id: UUID
    version_no: int
    status: str


class Author(BaseModel):
    actor_id: str
    display_name: str | None


class Impact(BaseModel):
    storyboard_count: int
    shot_count: int
    director_run_count: int
    storyboards: list[Reference]


class VersionSnapshot(Reference):
    created_at: datetime
    content_json: dict
    origin: Literal["manual", "generated", "unknown"]
    author: Author | None
    base_version: Reference | None
    script_version: Reference | None
    run_id: UUID | None
    impact: Impact


class Comparison(BaseModel):
    base: VersionSnapshot | None
    target: VersionSnapshot


def require_version(
    db: Session, model: type[Script] | type[Storyboard], episode_id: UUID, version_id: UUID
) -> Script | Storyboard:
    row = cast(Script | Storyboard | None, db.get(model, version_id))
    if row is None or row.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "内容版本不存在", 404)
    return row


def snapshot(db: Session, row: Script | Storyboard, project_id: UUID) -> VersionSnapshot:
    kind = "script" if isinstance(row, Script) else "storyboard"
    event = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.project_id == project_id,
            AuditEvent.resource_id == row.id,
            AuditEvent.action == f"{kind}.version_created",
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(1)
    )
    base = None
    if event and event.details_json.get("base_version_id"):
        model = Script if isinstance(row, Script) else Storyboard
        try:
            candidate = cast(
                Script | Storyboard | None,
                db.get(model, UUID(event.details_json["base_version_id"])),
            )
        except (ValueError, TypeError, AttributeError):
            candidate = None
        if candidate and candidate.episode_id == row.episode_id:
            base = Reference.model_validate(candidate, from_attributes=True)
    if row.run_id:
        event = db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.project_id == project_id,
                AuditEvent.resource_id == row.run_id,
                AuditEvent.action == "workflow.started",
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
            .limit(1)
        )
    author = None
    if event:
        try:
            user = db.get(User, UUID(event.actor_id))
        except ValueError:
            user = None
        author = Author(actor_id=event.actor_id, display_name=user.display_name if user else None)
    boards = select(Storyboard).where(Storyboard.episode_id == row.episode_id)
    if isinstance(row, Script):
        boards = boards.where(Storyboard.script_version_id == row.id)
        shot_ids = select(Storyboard.id).where(
            Storyboard.episode_id == row.episode_id, Storyboard.script_version_id == row.id
        )
        shot_filter: ColumnElement[bool] = Shot.storyboard_id.in_(shot_ids)
        run_count = (
            db.scalar(
                select(func.count())
                .select_from(WorkflowRun)
                .where(
                    WorkflowRun.episode_id == row.episode_id,
                    WorkflowRun.workflow_type == "storyboard",
                    WorkflowRun.input_json["script_version_id"].as_string() == str(row.id),
                )
            )
            or 0
        )
        board_count = db.scalar(select(func.count()).select_from(boards.subquery())) or 0
        previews = list(db.scalars(boards.order_by(Storyboard.version_no.desc()).limit(20)))
        script = None
    else:
        shot_filter = Shot.storyboard_id == row.id
        board_count, run_count, previews = 0, 0, []
        script = require_version(db, Script, row.episode_id, row.script_version_id)
    return VersionSnapshot(
        id=row.id,
        version_no=row.version_no,
        status=row.status,
        created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC),
        content_json=row.content_json,
        origin="generated" if row.run_id else "manual" if author else "unknown",
        author=author,
        base_version=base,
        script_version=Reference.model_validate(script, from_attributes=True) if script else None,
        run_id=row.run_id,
        impact=Impact(
            storyboard_count=board_count,
            shot_count=db.scalar(select(func.count()).select_from(Shot).where(shot_filter)) or 0,
            director_run_count=run_count,
            storyboards=[
                Reference.model_validate(board, from_attributes=True) for board in previews
            ],
        ),
    )


@router.get("/episodes/{episode_id}/{resource_type}/{version_id}/comparison")
def compare_versions(
    episode_id: UUID,
    resource_type: Literal["scripts", "storyboards"],
    version_id: UUID,
    db: Database,
    actor: Actor,
    base_version_id: UUID | None = None,
) -> Comparison:
    episode = require_episode(db, episode_id, actor)
    model: type[Script] | type[Storyboard] = Script if resource_type == "scripts" else Storyboard
    base: Script | Storyboard | None
    target = require_version(db, model, episode_id, version_id)
    if base_version_id is not None:
        base = require_version(db, model, episode_id, base_version_id)
    else:
        base = cast(
            Script | Storyboard | None,
            db.scalar(
                select(model)
                .where(model.episode_id == episode_id, model.version_no < target.version_no)
                .order_by(model.version_no.desc())
                .limit(1)
            ),
        )
    return Comparison(
        base=snapshot(db, base, episode.project_id) if base else None,
        target=snapshot(db, target, episode.project_id),
    )
