"""Authorized task policy, bounded preview and frozen run input reads."""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Request

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_project, require_run
from arclight.application.context_builder import build_context
from arclight.application.context_snapshots import manifest_references, read_contexts
from arclight.domain.context import BUILDER_VERSION, TASK_CONTRACTS, ContextRequest

router = APIRouter(prefix="/api/v1", tags=["context"])


@router.get("/projects/{project_id}/context-policy")
def read_policy(project_id: UUID, request: Request, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    return {
        "builder_version": BUILDER_VERSION,
        "limits": request.app.state.settings.context_limits.model_dump(),
        "tasks": {task: asdict(contract) for task, contract in TASK_CONTRACTS.items()},
        "overflow": "REJECT_REQUIRED_INPUT",
    }


@router.post("/projects/{project_id}/contexts/preview")
def preview_context(
    project_id: UUID, payload: ContextRequest, request: Request, db: Database, actor: Actor
) -> dict:
    return build_context(db, project_id, actor, payload, request.app.state.settings)


@router.get("/runs/{run_id}/contexts")
def read_run_contexts(run_id: UUID, db: Database, actor: Actor) -> dict:
    run = require_run(db, run_id, actor)
    from arclight.application.commands import require_episode

    episode = require_episode(db, run.episode_id, actor)
    references = manifest_references(db, episode.project_id, run.id)
    return {
        "run_id": str(run.id),
        "legacy_without_context": references is None,
        "manifest_ids": references or {},
        "contexts": read_contexts(db, episode.project_id, run),
    }
