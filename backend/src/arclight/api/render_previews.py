"""Small deterministic still-preview entry point with durable Worker dispatch."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.rendering import create_preview
from arclight.core.errors import DomainError
from arclight.persistence.models import RenderExecution, RenderJob

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["render-previews"])


class PreviewCreate(Input):
    retry_of: UUID | None = None


class PreviewRead(Resource):
    input_file_id: UUID
    retry_of_id: UUID | None
    status: str
    output_file_id: UUID | None
    error_code: str | None
    finished_at: datetime | None


@router.post("/files/{file_id}/render-previews", status_code=202)
def create(
    project_id: UUID,
    file_id: UUID,
    payload: PreviewCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> PreviewRead:
    row = create_preview(
        db, request.app.state.settings, project_id, file_id, actor, retry_of=payload.retry_of
    )
    audit(
        db, project_id, actor, "render.preview_requested", row.id, {"input_file_id": str(file_id)}
    )
    response.headers["Cache-Control"] = "no-store"
    return PreviewRead.model_validate(row)


@router.get("/render-previews/{run_id}")
def read(
    project_id: UUID, run_id: UUID, db: Database, actor: Actor, response: Response
) -> PreviewRead:
    require_project(db, project_id, actor)
    row = db.get(RenderExecution, run_id)
    if row is None or row.project_id != project_id or db.get(RenderJob, run_id) is not None:
        raise DomainError("NOT_FOUND", "渲染任务不存在", 404)
    response.headers["Cache-Control"] = "no-store"
    return PreviewRead.model_validate(row)
