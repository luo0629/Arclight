from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.producer_interaction import (
    queue_answer,
    queue_command,
    start_interactive_producer,
)
from arclight.application.producer_progress import read_progress
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.producer_interaction import ProducerCommand, ProducerRunCreate

router = APIRouter(prefix="/api/v1", tags=["producer"])
CommandKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


def _accepted(run) -> AcceptedRun:
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.post("/episodes/{episode_id}/producer-runs", status_code=202)
def create(
    episode_id: UUID,
    payload: ProducerRunCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: CommandKey,
) -> AcceptedRun:
    return _accepted(
        start_interactive_producer(
            db,
            request.app.state.settings,
            episode_id,
            actor,
            payload,
            key,
            request.state.request_id,
        )
    )


@router.get("/runs/{run_id}/producer")
def progress(run_id: UUID, request: Request, db: Database, actor: Actor) -> dict:
    return read_progress(db, run_id, actor, request.app.state.settings)


@router.post("/runs/{run_id}/producer/commands", status_code=202)
def command(
    run_id: UUID,
    payload: ProducerCommand,
    request: Request,
    db: Database,
    actor: Actor,
    key: CommandKey,
) -> AcceptedRun:
    return _accepted(queue_command(db, request.app.state.settings, run_id, actor, payload, key))


@router.post("/runs/{run_id}/producer/{loop_id}/answers", status_code=202)
def answer(
    run_id: UUID, loop_id: UUID, payload: AgentHumanDecision, db: Database, actor: Actor
) -> AcceptedRun:
    return _accepted(queue_answer(db, run_id, loop_id, actor, payload))
