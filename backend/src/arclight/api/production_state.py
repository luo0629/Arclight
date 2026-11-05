from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.application.production_state import read_production_state
from arclight.domain.production_state import EpisodeProductionState

router = APIRouter(prefix="/api/v1", tags=["production"])


@router.get("/episodes/{episode_id}/production-state")
def get_production_state(episode_id: UUID, db: Database, actor: Actor) -> EpisodeProductionState:
    return read_production_state(db, episode_id, actor)
