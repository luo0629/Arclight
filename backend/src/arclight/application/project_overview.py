"""Read-only project aggregates over persisted metadata, without loading story payloads."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from arclight.api.schemas import EpisodeRead
from arclight.application.commands import require_project
from arclight.application.costs import read_costs
from arclight.domain.costs import CostTotals
from arclight.persistence.models import (
    Episode,
    Script,
    StoryAnalysis,
    Storyboard,
    StoryOutline,
    WorkflowRun,
)


class ReviewCounts(BaseModel):
    workflows: int
    manual_scripts: int
    manual_storyboards: int
    outline_confirmations: int
    total: int


class CostCoverage(CostTotals):
    status: Literal["AVAILABLE"] = "AVAILABLE"
    budget_limit: Decimal
    currency: Literal["USD"] = "USD"


class DeliveryCoverage(BaseModel):
    status: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    version_count: None = None


class OverviewEpisode(EpisodeRead):
    stage: str
    production_state_url: str


class RecentTask(BaseModel):
    id: UUID
    episode_id: UUID
    episode_no: int
    episode_title: str
    workflow_type: str
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectOverview(BaseModel):
    project_id: UUID
    project_name: str
    archived: bool
    generated_at: datetime
    episode_count: int
    active_episode_count: int
    archived_episode_count: int
    stage_counts: dict[str, int]
    task_counts: dict[str, int]
    task_total: int
    pending_reviews: ReviewCounts
    cost: CostCoverage
    deliveries: DeliveryCoverage
    episodes: list[OverviewEpisode]
    limit: int
    offset: int
    recent_tasks: list[RecentTask]


def _episode_stages(project_id: UUID):
    # Ranking selects exact latest versions; an old approved script must not make
    # a newly rejected/reviewable script or an obsolete storyboard look complete.
    scripts = (
        select(
            Script.id,
            Script.episode_id,
            Script.status,
            func.row_number()
            .over(partition_by=Script.episode_id, order_by=Script.version_no.desc())
            .label("position"),
        )
        .join(Episode, Episode.id == Script.episode_id)
        .where(Episode.project_id == project_id)
        .subquery()
    )
    boards = (
        select(
            Storyboard.episode_id,
            Storyboard.script_version_id,
            Storyboard.status,
            func.row_number()
            .over(partition_by=Storyboard.episode_id, order_by=Storyboard.version_no.desc())
            .label("position"),
        )
        .join(Episode, Episode.id == Storyboard.episode_id)
        .where(Episode.project_id == project_id)
        .subquery()
    )
    outline = (
        select(StoryOutline.status)
        .where(StoryOutline.episode_id == Episode.id, StoryOutline.project_id == project_id)
        .order_by(StoryOutline.version_no.desc())
        .limit(1)
        .scalar_subquery()
    )
    analysis = select(StoryAnalysis.id).where(StoryAnalysis.episode_id == Episode.id).exists()
    stage = case(
        (Episode.status == "ARCHIVED", "ARCHIVED"),
        (scripts.c.status == "READY_FOR_REVIEW", "SCRIPT_REVIEW"),
        (scripts.c.status == "REJECTED", "SCRIPT_REVISION"),
        (
            (scripts.c.status == "APPROVED")
            & (boards.c.script_version_id == scripts.c.id)
            & (boards.c.status == "APPROVED"),
            "STORYBOARD_APPROVED",
        ),
        (
            (scripts.c.status == "APPROVED")
            & (boards.c.script_version_id == scripts.c.id)
            & (boards.c.status == "READY_FOR_REVIEW"),
            "STORYBOARD_REVIEW",
        ),
        (
            (scripts.c.status == "APPROVED")
            & (boards.c.script_version_id == scripts.c.id)
            & (boards.c.status == "REJECTED"),
            "STORYBOARD_REVISION",
        ),
        (scripts.c.status == "APPROVED", "SCRIPT_APPROVED"),
        (scripts.c.id.is_not(None), "SCRIPT_DRAFT"),
        (outline == "APPROVED", "OUTLINE_APPROVED"),
        (outline == "DRAFT", "OUTLINE_DRAFT"),
        (analysis, "ANALYSIS_READY"),
        else_="NOT_STARTED",
    ).label("stage")
    return (
        select(Episode, stage)
        .outerjoin(scripts, (scripts.c.episode_id == Episode.id) & (scripts.c.position == 1))
        .outerjoin(boards, (boards.c.episode_id == Episode.id) & (boards.c.position == 1))
        .where(Episode.project_id == project_id)
    )


def project_overview(
    db: Session, project_id: UUID, actor: str, *, limit: int, offset: int
) -> ProjectOverview:
    project = require_project(db, project_id, actor)
    stages = _episode_stages(project_id)
    stage_rows = stages.with_only_columns(Episode.id, stages.selected_columns.stage).subquery()
    all_stages: dict[str, int] = {
        stage: count
        for stage, count in db.execute(
            select(stage_rows.c.stage, func.count()).group_by(stage_rows.c.stage)
        )
    }
    archived_count = all_stages.pop("ARCHIVED", 0)
    active_episodes = select(Episode.id).where(
        Episode.project_id == project_id, Episode.status != "ARCHIVED"
    )
    task_counts: dict[str, int] = {
        status: count
        for status, count in db.execute(
            select(WorkflowRun.status, func.count())
            .where(WorkflowRun.episode_id.in_(active_episodes))
            .group_by(WorkflowRun.status)
        )
    }

    def manual_count(model: type[Script] | type[Storyboard]) -> int:
        return (
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.episode_id.in_(active_episodes),
                    model.run_id.is_(None),
                    model.status == "READY_FOR_REVIEW",
                )
            )
            or 0
        )

    manual_scripts = manual_count(Script)
    manual_boards = manual_count(Storyboard)
    outlines = (
        db.scalar(
            select(func.count())
            .select_from(StoryOutline)
            .where(
                StoryOutline.project_id == project_id,
                StoryOutline.episode_id.in_(active_episodes),
                StoryOutline.status == "DRAFT",
            )
        )
        or 0
    )
    workflows = task_counts.get("WAITING_REVIEW", 0)
    rows = db.execute(stages.order_by(Episode.episode_no, Episode.id).limit(limit).offset(offset))
    episodes = [
        OverviewEpisode(
            **EpisodeRead.model_validate(episode).model_dump(),
            stage=stage,
            production_state_url=f"/api/v1/episodes/{episode.id}/production-state",
        )
        for episode, stage in rows
    ]
    tasks = db.execute(
        select(
            WorkflowRun.id,
            WorkflowRun.episode_id,
            Episode.episode_no,
            Episode.title.label("episode_title"),
            WorkflowRun.workflow_type,
            WorkflowRun.status,
            WorkflowRun.created_at,
            WorkflowRun.updated_at,
        )
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(Episode.project_id == project_id, Episode.status != "ARCHIVED")
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(10)
    ).mappings()
    return ProjectOverview(
        project_id=project.id,
        project_name=project.name,
        archived=project.archived,
        generated_at=datetime.now(UTC),
        episode_count=sum(all_stages.values()) + archived_count,
        active_episode_count=sum(all_stages.values()),
        archived_episode_count=archived_count,
        stage_counts=all_stages,
        task_counts=task_counts,
        task_total=sum(task_counts.values()),
        pending_reviews=ReviewCounts(
            workflows=workflows,
            manual_scripts=manual_scripts,
            manual_storyboards=manual_boards,
            outline_confirmations=outlines,
            total=workflows + manual_scripts + manual_boards + outlines,
        ),
        cost=CostCoverage(
            budget_limit=project.budget_limit,
            **read_costs(db, project_id, actor).summary.model_dump(),
        ),
        deliveries=DeliveryCoverage(),
        episodes=episodes,
        limit=limit,
        offset=offset,
        recent_tasks=[RecentTask.model_validate(task) for task in tasks],
    )
