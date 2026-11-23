"""Read-only narrative previews and queries against an authorized frozen run."""

from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_episode
from arclight.application.writing_context import (
    capture_narrative_context,
    load_narrative_projection,
    query_frozen_writing,
)
from arclight.domain.script_narrative import FrozenWritingQuery, NarrativeRequest

router = APIRouter(prefix="/api/v1/episodes/{episode_id}/writing-context", tags=["writing-context"])


@router.post("")
def preview(episode_id: UUID, payload: NarrativeRequest, db: Database, actor: Actor) -> dict:
    episode = require_episode(db, episode_id, actor)
    context = capture_narrative_context(db, episode, actor, payload)
    projection = load_narrative_projection(db, episode.project_id, actor, context)
    identities = context["character_ids"]
    return {
        "context": context,
        "projection": projection,
        "character_projections": [
            load_narrative_projection(
                db, episode.project_id, actor, context, character_id=UUID(identity), limit=10
            )
            for identity in identities[:8]
        ],
        "character_projections_truncated": len(identities) > 8,
    }


@router.post("/query")
def query(episode_id: UUID, payload: FrozenWritingQuery, db: Database, actor: Actor) -> dict:
    return query_frozen_writing(db, episode_id, actor, payload)
