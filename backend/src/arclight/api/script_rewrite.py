from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from arclight.agents.script_rewrite import RewriteRequest
from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.script_rewrite import start_rewrite

router = APIRouter(prefix="/api/v1/episodes/{episode_id}", tags=["script rewrite"])


@router.post("/script-rewrite-runs", status_code=202)
def create_rewrite(
    episode_id: UUID,
    payload: RewriteRequest,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    run = start_rewrite(
        db, episode_id, actor, payload, key, request.state.request_id, request.app.state.settings
    )
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )
