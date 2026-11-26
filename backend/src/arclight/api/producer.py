from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.commands import require_project
from arclight.application.producer_interaction import (
    queue_answer,
    queue_command,
    start_interactive_producer,
)
from arclight.application.producer_progress import read_progress
from arclight.application.producer_series_delivery import complete_series, control_series
from arclight.application.short_drama import (
    read_short_drama,
    short_drama_preflight,
    start_short_drama,
)
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.producer_interaction import ProducerCommand, ProducerRunCreate
from arclight.domain.producer_series import SeriesCompletion, SeriesControl, SeriesProducerCreate
from arclight.domain.short_drama import ShortDramaCreate, ShortDramaPreflight, ShortDramaRetry

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


@router.post("/episodes/{episode_id}/short-drama", status_code=202)
def create_short_drama(
    episode_id: UUID,
    payload: ShortDramaCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: CommandKey,
) -> AcceptedRun:
    return _accepted(
        start_short_drama(
            db,
            request.app.state.settings,
            episode_id,
            actor,
            payload,
            key,
            request.state.request_id,
        )
    )


@router.post("/runs/{run_id}/short-drama/retry", status_code=202)
def retry_short_drama_run(
    run_id: UUID,
    payload: ShortDramaRetry,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    from arclight.application.short_drama_production import retry_short_drama

    return _accepted(
        retry_short_drama(
            db, request.app.state.settings, run_id, payload.expected_revision, actor, key
        )
    )


@router.get("/episodes/{episode_id}/short-drama")
def short_drama_progress(episode_id: UUID, db: Database, actor: Actor) -> dict | None:
    return read_short_drama(db, episode_id, actor)


@router.get("/episodes/{episode_id}/short-drama/preflight")
def short_drama_readiness(
    episode_id: UUID, script_version_id: UUID, request: Request, db: Database, actor: Actor
) -> ShortDramaPreflight:
    return short_drama_preflight(
        db, request.app.state.settings, episode_id, actor, script_version_id
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


@router.post("/projects/{project_id}/producer-runs", status_code=202)
def create_series(
    project_id: UUID,
    payload: SeriesProducerCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: CommandKey,
) -> AcceptedRun:
    require_project(db, project_id, actor, write=True)
    from arclight.application.commands import require_episode
    from arclight.core.errors import DomainError

    anchor = require_episode(db, payload.scope.episodes[0].episode_id, actor, write=True)
    if anchor.project_id != project_id:
        raise DomainError("NOT_FOUND", "同项目剧集不存在", 404)
    return _accepted(
        start_interactive_producer(
            db,
            request.app.state.settings,
            anchor.id,
            actor,
            payload,
            key,
            request.state.request_id,
        )
    )


@router.post("/runs/{run_id}/producer/series-controls", status_code=202)
def series_control(
    run_id: UUID,
    payload: SeriesControl,
    db: Database,
    actor: Actor,
    key: CommandKey,
) -> AcceptedRun:
    return _accepted(control_series(db, run_id, actor, payload, key))


@router.post("/runs/{run_id}/producer/completion", status_code=202)
def series_completion(
    run_id: UUID,
    payload: SeriesCompletion,
    db: Database,
    actor: Actor,
) -> AcceptedRun:
    return _accepted(complete_series(db, run_id, actor, payload))
