"""Read precise shot production state; mutations retain their original endpoints."""

from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.application.shot_production import read_production
from arclight.domain.shot_production import StoryboardProduction

router = APIRouter(
    prefix="/api/v1/episodes/{episode_id}/storyboards/{storyboard_id}",
    tags=["storyboard-production"],
)


@router.get("/production")
def get_production(
    episode_id: UUID,
    storyboard_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 50,
    offset: Offset = 0,
    shot_id: UUID | None = None,
) -> StoryboardProduction:
    return read_production(
        db, episode_id, storyboard_id, actor, limit=limit, offset=offset, shot_id=shot_id
    )
