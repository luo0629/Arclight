"""Permission-scoped revision commands; HTTP never calls a media Provider."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from arclight.api.dependencies import Actor, Database
from arclight.application.commands import require_project
from arclight.application.continuity_feedback_store import feedback_history
from arclight.application.continuity_revision_store import revision_record
from arclight.application.continuity_revisions import request_revision
from arclight.domain.continuity_feedback import FeedbackRecord
from arclight.domain.continuity_revisions import RevisionRecord, RevisionRequest

router = APIRouter(prefix="/api/v1/projects/{project_id}/continuity-revisions", tags=["continuity"])


@router.post("", status_code=201)
def create_revision(
    project_id: UUID,
    payload: RevisionRequest,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160, pattern=r"^[!-~]+$")],
) -> RevisionRecord:
    record, created = request_revision(
        db, request.app.state.settings, project_id, actor, idempotency_key, payload
    )
    response.status_code = 201 if created else 200
    return record


@router.get("/{revision_id}")
def get_revision(project_id: UUID, revision_id: UUID, db: Database, actor: Actor) -> RevisionRecord:
    require_project(db, project_id, actor)
    return revision_record(db, project_id, revision_id)


@router.get("/{revision_id}/feedback")
def get_feedback(
    project_id: UUID, revision_id: UUID, db: Database, actor: Actor
) -> list[FeedbackRecord]:
    require_project(db, project_id, actor)
    revision_record(db, project_id, revision_id)
    return feedback_history(db, project_id, revision_id)
