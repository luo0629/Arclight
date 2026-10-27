"""Identity reconciliation is independent of content approval and visual asset creation."""

from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.application.character_matching import CharacterDecision, SourceType, confirm, preview

router = APIRouter(prefix="/api/v1", tags=["characters"])


@router.get("/episodes/{episode_id}/{source_type}/{version_id}/character-matches")
def character_matches(
    episode_id: UUID, source_type: SourceType, version_id: UUID, db: Database, actor: Actor
) -> dict:
    return preview(db, episode_id, source_type, version_id, actor)


@router.post("/episodes/{episode_id}/{source_type}/{version_id}/character-matches")
def confirm_character(
    episode_id: UUID,
    source_type: SourceType,
    version_id: UUID,
    payload: CharacterDecision,
    db: Database,
    actor: Actor,
) -> dict:
    return confirm(db, episode_id, source_type, version_id, actor, payload)
