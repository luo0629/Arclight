"""Read-only overview of the approved series plan and persisted Producer runs."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.producer_series_delivery import paused
from arclight.application.story_ledger import get_head
from arclight.domain.producer_series import SERIES_PRODUCER_VERSION
from arclight.domain.series_planning import SeriesOutlineContent
from arclight.persistence.models import Episode, StoryOutline, WorkflowRun


class SeriesCharacter(BaseModel):
    id: UUID
    name: str


class ApprovedSeriesPlan(BaseModel):
    id: UUID
    episode_id: UUID
    version_no: int
    content: SeriesOutlineContent
    characters: list[SeriesCharacter]


class SeriesRunSummary(BaseModel):
    id: UUID
    project_id: UUID
    episode_id: UUID
    episode_no: int
    status: str
    paused: bool
    created_at: datetime


class SeriesWorkspace(BaseModel):
    project_id: UUID
    plan: ApprovedSeriesPlan | None
    ledger_version_id: UUID | None
    ledger_head_revision: int
    items: list[SeriesRunSummary]
    total: int
    limit: int
    offset: int


def approved_series_plan(db: Session, project_id: UUID) -> ApprovedSeriesPlan | None:
    row = db.scalar(
        select(StoryOutline)
        .join(Episode, Episode.id == StoryOutline.episode_id)
        .where(
            StoryOutline.project_id == project_id,
            Episode.project_id == project_id,
            StoryOutline.status == "APPROVED",
            StoryOutline.content_json["schema_version"].as_string() == "2.0",
        )
        .order_by(StoryOutline.confirmed_at.desc(), StoryOutline.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return ApprovedSeriesPlan(
        id=row.id,
        episode_id=row.episode_id,
        version_no=row.version_no,
        content=SeriesOutlineContent.model_validate(row.content_json),
        characters=[
            SeriesCharacter.model_validate(reference)
            for reference in row.source_snapshot.get("series_plan_references", {}).get(
                "characters", []
            )
        ],
    )


def series_workspace(
    db: Session, project_id: UUID, actor: str, *, limit: int, offset: int
) -> SeriesWorkspace:
    require_project(db, project_id, actor)
    plan = approved_series_plan(db, project_id)
    head = get_head(db, project_id)
    runs = (
        select(WorkflowRun, Episode.episode_no)
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(
            Episode.project_id == project_id,
            WorkflowRun.workflow_type == "producer",
            WorkflowRun.workflow_version == SERIES_PRODUCER_VERSION,
        )
    )
    total = db.scalar(select(func.count()).select_from(runs.subquery())) or 0
    rows = db.execute(
        runs.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return SeriesWorkspace(
        project_id=project_id,
        plan=plan,
        ledger_version_id=head.approved_version_id if head else None,
        ledger_head_revision=head.revision if head else 0,
        total=total,
        limit=limit,
        offset=offset,
        items=[
            SeriesRunSummary(
                id=run.id,
                project_id=project_id,
                episode_id=run.episode_id,
                episode_no=episode_no,
                status=run.status,
                paused=run.status == "RUNNING" and paused(db, run),
                created_at=run.created_at,
            )
            for run, episode_no in rows
        ],
    )
