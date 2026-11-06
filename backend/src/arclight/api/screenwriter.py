from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.screenwriter_progress import queue_answer, read_progress
from arclight.domain.agent_runtime import AgentHumanDecision

router = APIRouter(prefix="/api/v1/runs/{run_id}/screenwriter", tags=["screenwriter"])


@router.get("")
def progress(run_id: UUID, db: Database, actor: Actor) -> dict:
    return read_progress(db, run_id, actor)


@router.post("/{loop_id}/answers", status_code=202)
def answer(
    run_id: UUID, loop_id: UUID, payload: AgentHumanDecision, db: Database, actor: Actor
) -> AcceptedRun:
    run = queue_answer(db, run_id, loop_id, actor, payload)
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )
