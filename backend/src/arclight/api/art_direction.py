"""Asynchronous art generation and original-loop human answers."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.agent_workflows import queue_answer
from arclight.application.art_workflow import read_progress
from arclight.application.commands import start_run
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.art_workflow import ArtRunCreate

router = APIRouter(prefix="/api/v1", tags=["art-direction"])


@router.post("/episodes/{episode_id}/art-runs", status_code=202)
def create(
    episode_id: UUID,
    payload: ArtRunCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    run = start_run(
        db,
        episode_id,
        actor,
        "",
        key,
        request.state.request_id,
        settings=request.app.state.settings,
        script_version_id=payload.script_version_id,
        art_task=payload.task,
        art_concept_plan=payload.concept_plan,
        agent_limits=payload.agent_limits,
    )
    return accepted(run)


def accepted(run) -> AcceptedRun:
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.get("/runs/{run_id}/art")
def progress(run_id: UUID, db: Database, actor: Actor) -> dict:
    return read_progress(db, run_id, actor)


@router.post("/runs/{run_id}/art/{loop_id}/answers", status_code=202)
def answer(
    run_id: UUID, loop_id: UUID, payload: AgentHumanDecision, db: Database, actor: Actor
) -> AcceptedRun:
    return accepted(queue_answer(db, run_id, loop_id, actor, payload, agent_name="art_director"))
