"""Authorized task policy, bounded preview and frozen run input reads."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_project, require_run
from arclight.application.context_builder import build_context
from arclight.application.context_snapshots import manifest_references, read_contexts
from arclight.domain.context import BUILDER_VERSION, TASK_CONTRACTS, ContextRequest
from arclight.domain.context_trace import MAX_EVIDENCE_ROUNDS, EvidenceRequest

router = APIRouter(prefix="/api/v1", tags=["context"])


@router.get("/projects/{project_id}/context-policy")
def read_policy(project_id: UUID, request: Request, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    return {
        "builder_version": BUILDER_VERSION,
        "limits": request.app.state.settings.context_limits.model_dump(),
        "tasks": {task: asdict(contract) for task, contract in TASK_CONTRACTS.items()},
        "overflow": "REJECT_REQUIRED_INPUT",
        "max_evidence_rounds": MAX_EVIDENCE_ROUNDS,
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


@router.get("/runs/{run_id}/contexts/{stage}/trace")
def get_trace(
    run_id: UUID,
    stage: str,
    db: Database,
    actor: Actor,
    through_round: Annotated[int | None, Query(ge=0, le=MAX_EVIDENCE_ROUNDS)] = None,
) -> dict:
    from arclight.application.context_evidence import read_trace

    run = require_run(db, run_id, actor)
    return read_trace(db, run, stage, actor, through_round)


@router.post("/runs/{run_id}/contexts/{stage}/evidence")
def add_evidence(
    run_id: UUID,
    stage: str,
    payload: EvidenceRequest,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> dict:
    from arclight.application.context_evidence import append_evidence

    run = require_run(db, run_id, actor)
    row, created = append_evidence(db, run, stage, actor, payload, request.app.state.settings)
    response.status_code = 201 if created else 200
    return {"manifest_id": str(row.id), **row.snapshot_json["context"]}


@router.get("/runs/{run_id}/contexts/{stage}/images/{file_id}")
def read_context_image(
    run_id: UUID,
    stage: str,
    file_id: UUID,
    request: Request,
    db: Database,
    actor: Actor,
) -> Response:
    from arclight.application.context_images import load_context_images

    run = require_run(db, run_id, actor)
    image = load_context_images(db, request.app.state.settings, run, stage, actor, [file_id])[
        file_id
    ]
    return Response(
        image.data,
        media_type=image.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="context-image"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
