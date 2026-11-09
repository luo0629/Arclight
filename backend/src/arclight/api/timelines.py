"""Project-authorized creation and exact historical timeline reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_episode
from arclight.application.timelines import create_timeline, read_timeline
from arclight.domain.timelines import TimelineCreate, TimelineRead
from arclight.persistence.timelines import Timeline

router = APIRouter(prefix="/api/v1", tags=["timelines"])


@router.post("/episodes/{episode_id}/timelines", status_code=201)
def create(
    episode_id: UUID, payload: TimelineCreate, request: Request, db: Database, actor: Actor
) -> TimelineRead:
    return create_timeline(db, request.app.state.settings, episode_id, actor, payload)


@router.get("/episodes/{episode_id}/timelines")
def versions(
    episode_id: UUID,
    db: Database,
    actor: Actor,
    before_version: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[TimelineRead]:
    require_episode(db, episode_id, actor)
    query = select(Timeline).where(Timeline.episode_id == episode_id, Timeline.sealed.is_(True))
    if before_version is not None:
        query = query.where(Timeline.version_no < before_version)
    rows = db.scalars(query.order_by(Timeline.version_no.desc()).limit(limit))
    return [read_timeline(db, row.id, actor) for row in rows]


@router.get("/timelines/{timeline_id}")
def get(timeline_id: UUID, db: Database, actor: Actor) -> TimelineRead:
    return read_timeline(db, timeline_id, actor)
