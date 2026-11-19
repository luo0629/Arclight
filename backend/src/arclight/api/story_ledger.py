"""Project story-ledger authoring and bounded perspective queries."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import Field
from sqlalchemy import func, select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.application.story_ledger import (
    approve_version,
    create_version,
    get_head,
    head_data,
    ledger_approval_impact,
    list_sources,
    query_version,
    require_version,
    version_approval,
    version_data,
    version_summary,
)
from arclight.domain.story_ledger import LedgerQuery, StoryLedgerContent, StoryLedgerContentV2
from arclight.persistence.models import StoryLedgerVersion

router = APIRouter(prefix="/api/v1/projects/{project_id}/story-ledger", tags=["story-ledger"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class LedgerCreate(Input):
    expected_revision: int = Field(strict=True, ge=0)
    base_version_id: UUID | None = None
    content: StoryLedgerContent | StoryLedgerContentV2


class LedgerApprove(Input):
    expected_revision: int = Field(strict=True, ge=0)
    impact_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class LedgerQueryRequest(Input):
    version_id: UUID
    query: LedgerQuery
    limit: int = Field(default=50, strict=True, ge=1, le=100)
    offset: int = Field(default=0, strict=True, ge=0, le=1000)


@router.get("")
def read_head(project_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    return head_data(get_head(db, project_id))


@router.get("/sources")
def sources(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
    episode_id: UUID | None = None,
) -> dict:
    return list_sources(db, project_id, actor, limit, offset, episode_id)


@router.get("/versions")
def versions(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> dict:
    require_project(db, project_id, actor)
    condition = StoryLedgerVersion.project_id == project_id
    total = db.scalar(select(func.count()).select_from(StoryLedgerVersion).where(condition)) or 0
    rows = db.scalars(
        select(StoryLedgerVersion)
        .where(condition)
        .order_by(StoryLedgerVersion.version_no.desc(), StoryLedgerVersion.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [version_summary(row, version_approval(db, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/versions/{version_id}")
def version(project_id: UUID, version_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    row = require_version(db, project_id, version_id)
    return version_data(row, version_approval(db, row))


@router.post("/versions", status_code=201)
def create(
    project_id: UUID, payload: LedgerCreate, db: Database, actor: Actor, idempotency_key: Key
) -> dict:
    return create_version(
        db,
        project_id,
        actor,
        payload.expected_revision,
        payload.base_version_id,
        payload.content,
        idempotency_key,
    )


@router.get("/versions/{version_id}/approval-impact")
def ledger_impact(project_id: UUID, version_id: UUID, db: Database, actor: Actor) -> dict:
    return ledger_approval_impact(db, project_id, version_id, actor)


@router.post("/versions/{version_id}/approve")
def approve(
    project_id: UUID,
    version_id: UUID,
    payload: LedgerApprove,
    db: Database,
    actor: Actor,
    idempotency_key: Key,
) -> dict:
    return approve_version(
        db,
        project_id,
        version_id,
        actor,
        payload.expected_revision,
        idempotency_key,
        payload.impact_digest,
    )


@router.post("/query")
def query(project_id: UUID, payload: LedgerQueryRequest, db: Database, actor: Actor) -> dict:
    return query_version(
        db, project_id, actor, payload.version_id, payload.query, payload.limit, payload.offset
    )
