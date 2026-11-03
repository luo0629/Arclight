"""Authorized and paginated production cost reports, without private route data."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.application.costs import read_costs, read_entries
from arclight.domain.costs import CostDimension, CostEntries, CostReport

router = APIRouter(prefix="/api/v1/projects/{project_id}/costs", tags=["costs"])
ProviderFilter = Annotated[str | None, Query(min_length=1, max_length=100)]
ModelFilter = Annotated[str | None, Query(min_length=1, max_length=160)]


@router.get("")
def get_costs(
    project_id: UUID,
    db: Database,
    actor: Actor,
    group_by: CostDimension = "project",
    episode_id: UUID | None = None,
    task_id: UUID | None = None,
    provider: ProviderFilter = None,
    model: ModelFilter = None,
    unassigned_episode: bool = False,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CostReport:
    return read_costs(
        db,
        project_id,
        actor,
        group_by=group_by,
        episode_id=episode_id,
        task_id=task_id,
        provider=provider,
        model=model,
        unassigned_episode=unassigned_episode,
        limit=limit,
        offset=offset,
    )


@router.get("/entries")
def get_cost_entries(
    project_id: UUID,
    db: Database,
    actor: Actor,
    episode_id: UUID | None = None,
    task_id: UUID | None = None,
    provider: ProviderFilter = None,
    model: ModelFilter = None,
    unassigned_episode: bool = False,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CostEntries:
    return read_entries(
        db,
        project_id,
        actor,
        episode_id=episode_id,
        task_id=task_id,
        provider=provider,
        model=model,
        unassigned_episode=unassigned_episode,
        limit=limit,
        offset=offset,
    )
