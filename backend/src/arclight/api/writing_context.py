"""Read-only narrative previews and queries against an authorized frozen run."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.commands import require_episode
from arclight.application.writing_context import (
    capture_narrative_context,
    load_narrative_projection,
)
from arclight.core.errors import DomainError
from arclight.domain.script_narrative import NarrativeRequest
from arclight.domain.story_ledger import StoryTime
from arclight.persistence.models import WorkflowRun

router = APIRouter(prefix="/api/v1/episodes/{episode_id}/writing-context", tags=["writing-context"])


class FrozenWritingQuery(Input):
    run_id: UUID
    story_time: StoryTime | None = None
    scene_no: int | None = Field(default=None, strict=True, ge=1, le=50)
    character_id: UUID | None = None
    causal_ids: list[UUID] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=20, strict=True, ge=1, le=100)
    offset: int = Field(default=0, strict=True, ge=0, le=1000)


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
    episode = require_episode(db, episode_id, actor)
    run = db.get(WorkflowRun, payload.run_id)
    if run is None or run.episode_id != episode.id:
        raise DomainError("NOT_FOUND", "运行不存在", 404)
    context = run.input_json.get("narrative_context")
    if context is None:
        raise DomainError("NARRATIVE_CONTEXT_UNAVAILABLE", "该运行没有冻结叙事上下文", 409)
    return load_narrative_projection(
        db,
        episode.project_id,
        actor,
        context,
        story_time=payload.story_time,
        scene_no=payload.scene_no,
        character_id=payload.character_id,
        causal_ids=payload.causal_ids,
        limit=payload.limit,
        offset=payload.offset,
    )
