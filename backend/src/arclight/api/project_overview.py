from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from arclight.api.dependencies import Actor, Database
from arclight.application.project_overview import ProjectOverview, project_overview

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects/{project_id}/overview")
def get_overview(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectOverview:
    return project_overview(db, project_id, actor, limit=limit, offset=offset)
