"""Project production indexes; mutations remain in their original guarded APIs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import AwareDatetime

from arclight.api.dependencies import Actor, Database
from arclight.application.project_production import (
    pending_project_reviews,
    project_deliveries,
    project_tasks,
)
from arclight.domain.project_production import (
    PendingProjectReviewPage,
    ProjectDeliveryPage,
    ProjectTaskPage,
    PublicError,
    TaskStatus,
    TaskType,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["project-production"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/tasks")
def tasks(
    project_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
    episode_id: UUID | None = None,
    task_type: TaskType | None = None,
    status: TaskStatus | None = None,
    error_code: PublicError | None = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> ProjectTaskPage:
    response.headers["Cache-Control"] = "no-store"
    return project_tasks(
        db,
        project_id,
        actor,
        episode_id=episode_id,
        task_type=task_type,
        status=status,
        error_code=error_code,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )


@router.get("/pending-reviews")
def pending_reviews(
    project_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
    episode_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PendingProjectReviewPage:
    response.headers["Cache-Control"] = "no-store"
    return pending_project_reviews(
        db,
        project_id,
        actor,
        episode_id=episode_id,
        limit=limit,
        offset=offset,
    )


@router.get("/deliveries")
def deliveries(
    project_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
    episode_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> ProjectDeliveryPage:
    response.headers["Cache-Control"] = "no-store"
    return project_deliveries(
        db,
        project_id,
        actor,
        episode_id=episode_id,
        limit=limit,
        offset=offset,
    )
