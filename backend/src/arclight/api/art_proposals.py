"""Shared proposal preflight; persistence and human review are separate operations."""

from uuid import UUID

from fastapi import APIRouter

from arclight.api.dependencies import Actor, Database
from arclight.application.art_protection import compare_art_proposal
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.art_protection import ArtComparison

router = APIRouter(prefix="/api/v1/projects/{project_id}/art-proposals", tags=["art-proposals"])


@router.post("/compare")
def compare(project_id: UUID, payload: ArtProposal, db: Database, actor: Actor) -> ArtComparison:
    if payload.project_id != project_id:
        raise DomainError("ART_SCOPE_INVALID", "提案项目与请求路径不一致", 422)
    return compare_art_proposal(db, payload, actor)
