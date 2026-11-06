from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from arclight.agents.script_rewrite import RewriteRequest
from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.script_rewrite import start_rewrite
from arclight.domain.screenwriter_revision import ScreenwriterRevisionOptions

router = APIRouter(prefix="/api/v1/episodes/{episode_id}", tags=["script rewrite"])


class RewriteRunCreate(RewriteRequest, ScreenwriterRevisionOptions):
    pass


@router.post("/script-rewrite-runs", status_code=202)
def create_rewrite(
    episode_id: UUID,
    payload: RewriteRunCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    run = start_rewrite(
        db,
        episode_id,
        actor,
        RewriteRequest.model_validate(
            payload.model_dump(include={"base_version_id", "target", "instructions"})
        ),
        key,
        request.state.request_id,
        request.app.state.settings,
        professional=payload.professional,
        agent_limits=payload.agent_limits,
        revision_rounds=payload.revision_rounds,
    )
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )
