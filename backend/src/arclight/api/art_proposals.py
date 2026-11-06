"""Manual proposals and the shared, exact human review/approval entry point."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.human_reviews import HumanReviewRead, ReviewResource
from arclight.api.human_reviews import serialize as serialize_review
from arclight.api.routes import Limit, Offset
from arclight.application.art_proposal_store import (
    archive_proposal,
    asset_content,
    current_version,
    require_proposal,
    save_proposal,
)
from arclight.application.art_proposals import asset_baseline
from arclight.application.art_protection import compare_art_proposal
from arclight.application.art_reviews import decide_art, request_id, selection, submit_art
from arclight.application.commands import require_project
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.art_protection import ArtComparison, ArtConfirmation
from arclight.domain.art_review import ArtArchive, ArtDecision, ArtSave, ArtSubmit
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import (
    ArtApprovedVersion,
    ArtProposalRecord,
    ArtProposalVersion,
)
from arclight.persistence.human_reviews import HumanReviewRequest
from arclight.persistence.models import VisualAsset

router = APIRouter(prefix="/api/v1/projects/{project_id}/art-proposals", tags=["art-proposals"])


@router.post("/compare")
def compare(project_id: UUID, payload: ArtProposal, db: Database, actor: Actor) -> ArtComparison:
    if payload.project_id != project_id:
        raise DomainError("ART_SCOPE_INVALID", "提案项目与请求路径不一致", 422)
    return compare_art_proposal(db, payload, actor)


class ArtVersionRead(ReviewResource):
    proposal_id: UUID
    version_no: int
    created_by: str
    input_sha256: str
    proposal_json: ArtProposal
    comparison_json: ArtComparison
    baseline_json: dict


class ArtOutputRead(ReviewResource):
    proposal_version_id: UUID
    design_id: UUID
    target_type: str
    asset_id: UUID | None
    visual_bible_version_id: UUID | None
    version_no: int
    before_json: dict | None
    content_json: dict
    content_sha256: str
    created_by: str


class ArtRead(ReviewResource):
    project_id: UUID
    revision: int
    archived: bool
    current_version_no: int
    version: ArtVersionRead
    review: HumanReviewRead | None
    review_confirmation: ArtConfirmation | None
    outputs: list[ArtOutputRead]


def serialize(
    db: Database, row: ArtProposalRecord, version: ArtProposalVersion | None = None
) -> ArtRead:
    version = version or current_version(db, row)
    request = db.get(HumanReviewRequest, request_id(version, row.project_id))
    outputs = db.scalars(
        select(ArtApprovedVersion)
        .where(ArtApprovedVersion.proposal_version_id == version.id)
        .order_by(ArtApprovedVersion.created_at, ArtApprovedVersion.id)
    )
    return ArtRead(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        project_id=row.project_id,
        revision=row.revision,
        archived=row.archived,
        current_version_no=row.version_no,
        version=ArtVersionRead.model_validate(version),
        review=serialize_review(db, request) if request else None,
        review_confirmation=selection(db, version.id) if request else None,
        outputs=[ArtOutputRead.model_validate(output) for output in outputs],
    )


@router.get("/baselines/visual-bible")
def bible_baseline(project_id: UUID, db: Database, actor: Actor) -> dict:
    value = snapshot(db, require_project(db, project_id, actor))
    return {**value, "content_sha256": digest(value)}


@router.get("/baselines/assets/{asset_id}")
def directory_baseline(project_id: UUID, asset_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    row = db.get(VisualAsset, asset_id, populate_existing=True)
    if (
        row is None
        or row.project_id != project_id
        or row.asset_type
        not in {
            "CHARACTER",
            "SCENE",
            "PROP",
            "STYLE",
        }
    ):
        raise DomainError("NOT_FOUND", "视觉资产不存在", 404)
    return {"target": asset_baseline(row).model_dump(mode="json"), "content": asset_content(row)}


@router.get("")
def list_proposals(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
    include_archived: bool = False,
) -> list[ArtRead]:
    require_project(db, project_id, actor)
    query = select(ArtProposalRecord).where(ArtProposalRecord.project_id == project_id)
    if not include_archived:
        query = query.where(ArtProposalRecord.archived.is_(False))
    rows = db.scalars(
        query.order_by(ArtProposalRecord.created_at.desc(), ArtProposalRecord.id)
        .limit(limit)
        .offset(offset)
    )
    return [serialize(db, row) for row in rows]


def manual(payload: ArtSave) -> None:
    if payload.proposal.origin != "MANUAL":
        raise DomainError("ART_MANUAL_REQUIRED", "人工入口只能保存人工提案", 422)


@router.post("", status_code=201)
def create(
    project_id: UUID,
    payload: ArtSave,
    db: Database,
    actor: Actor,
    response: Response,
) -> ArtRead:
    manual(payload)
    row, version, created = save_proposal(db, project_id, actor, payload)
    response.status_code = 201 if created else 200
    return serialize(db, row, version)


@router.get("/{proposal_id}")
def get_proposal(project_id: UUID, proposal_id: UUID, db: Database, actor: Actor) -> ArtRead:
    return serialize(db, require_proposal(db, project_id, proposal_id, actor))


@router.put("/{proposal_id}", status_code=201)
def revise(
    project_id: UUID,
    proposal_id: UUID,
    payload: ArtSave,
    db: Database,
    actor: Actor,
    response: Response,
) -> ArtRead:
    manual(payload)
    row, version, created = save_proposal(db, project_id, actor, payload, proposal_id=proposal_id)
    response.status_code = 201 if created else 200
    return serialize(db, row, version)


@router.patch("/{proposal_id}")
def archive(
    project_id: UUID,
    proposal_id: UUID,
    payload: ArtArchive,
    db: Database,
    actor: Actor,
) -> ArtRead:
    return serialize(db, archive_proposal(db, project_id, proposal_id, actor, payload))


@router.delete("/{proposal_id}")
def delete_proposal(
    project_id: UUID,
    proposal_id: UUID,
    db: Database,
    actor: Actor,
    expected_revision: Annotated[int, Query(ge=0)],
) -> ArtRead:
    return archive(
        project_id,
        proposal_id,
        ArtArchive(expected_revision=expected_revision, archived=True),
        db,
        actor,
    )


@router.get("/{proposal_id}/versions")
def versions(
    project_id: UUID,
    proposal_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[ArtVersionRead]:
    require_proposal(db, project_id, proposal_id, actor)
    rows = db.scalars(
        select(ArtProposalVersion)
        .where(ArtProposalVersion.proposal_id == proposal_id)
        .order_by(ArtProposalVersion.version_no.desc())
        .limit(limit)
        .offset(offset)
    )
    return [ArtVersionRead.model_validate(row) for row in rows]


@router.get("/{proposal_id}/versions/{version_id}")
def get_version(
    project_id: UUID,
    proposal_id: UUID,
    version_id: UUID,
    db: Database,
    actor: Actor,
) -> ArtRead:
    row = require_proposal(db, project_id, proposal_id, actor)
    version = db.get(ArtProposalVersion, version_id)
    if version is None or version.proposal_id != row.id:
        raise DomainError("NOT_FOUND", "提案版本不存在", 404)
    return serialize(db, row, version)


@router.post("/{proposal_id}/submit")
def submit(
    project_id: UUID,
    proposal_id: UUID,
    payload: ArtSubmit,
    db: Database,
    actor: Actor,
) -> ArtRead:
    submit_art(db, project_id, proposal_id, actor, payload)
    return get_proposal(project_id, proposal_id, db, actor)


@router.post("/{proposal_id}/decisions")
def decide(
    project_id: UUID,
    proposal_id: UUID,
    payload: ArtDecision,
    db: Database,
    actor: Actor,
) -> ArtRead:
    decide_art(db, project_id, proposal_id, actor, payload)
    return get_proposal(project_id, proposal_id, db, actor)
