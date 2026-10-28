"""Typed visual direction edits, immutable history and Owner field locks."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.application.visual_bible import append_version, state
from arclight.core.errors import DomainError
from arclight.domain.visual_bible import FIELD_NAMES, VisualField, VisualFields, VisualPatch
from arclight.persistence.models import VisualBibleVersion

router = APIRouter(prefix="/api/v1/projects/{project_id}/visual-bible", tags=["visual-bible"])


class VisualRead(BaseModel):
    id: UUID | None
    version_no: int
    fields: VisualFields
    locked_fields: list[VisualField]
    created_by: str | None
    created_at: datetime | None
    change_kind: str
    legacy_snapshot: dict

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Edit(Input):
    expected_version_no: int = Field(strict=True, ge=0)
    creation_key: UUID
    fields: VisualPatch


class Locks(Input):
    expected_version_no: int = Field(strict=True, ge=0)
    creation_key: UUID
    locked_fields: list[VisualField] = Field(max_length=8)

    @model_validator(mode="after")
    def unique_locks(self) -> "Locks":
        if len(set(self.locked_fields)) != len(self.locked_fields):
            raise ValueError("锁定字段不能重复")
        return self


def read_version(row: VisualBibleVersion) -> VisualRead:
    return VisualRead(
        id=row.id,
        version_no=row.version_no,
        fields=VisualFields(**{name: getattr(row, name) for name in FIELD_NAMES}),
        locked_fields=row.locked_fields,
        created_by=row.created_by,
        created_at=row.created_at,
        change_kind=row.change_kind,
        legacy_snapshot=row.legacy_snapshot,
    )


@router.get("")
def current(project_id: UUID, db: Database, actor: Actor) -> VisualRead:
    project = require_project(db, project_id, actor)
    return VisualRead.model_validate(state(db, project))


@router.get("/versions")
def versions(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[VisualRead]:
    require_project(db, project_id, actor)
    rows = db.scalars(
        select(VisualBibleVersion)
        .where(VisualBibleVersion.project_id == project_id)
        .order_by(VisualBibleVersion.version_no)
        .limit(limit)
        .offset(offset)
    )
    return [read_version(row) for row in rows]


@router.get("/versions/{version_id}")
def version(project_id: UUID, version_id: UUID, db: Database, actor: Actor) -> VisualRead:
    require_project(db, project_id, actor)
    row = db.get(VisualBibleVersion, version_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "视觉规范版本不存在", 404)
    return read_version(row)


@router.post("/versions", status_code=201)
def edit(project_id: UUID, payload: Edit, db: Database, actor: Actor) -> VisualRead:
    project = require_project(db, project_id, actor, write=True)
    row, created = append_version(
        db,
        project,
        actor,
        payload.expected_version_no,
        payload.creation_key,
        patch=payload.fields.model_dump(exclude_unset=True),
    )
    if created:
        audit(db, project_id, actor, "visual_bible.saved", row.id, {"version_no": row.version_no})
    return read_version(row)


@router.post("/locks", status_code=201)
def lock(project_id: UUID, payload: Locks, db: Database, actor: Actor) -> VisualRead:
    project = require_project(db, project_id, actor, write=True, owner=True)
    row, created = append_version(
        db,
        project,
        actor,
        payload.expected_version_no,
        payload.creation_key,
        locks=list(payload.locked_fields),
    )
    if created:
        audit(
            db,
            project_id,
            actor,
            "visual_bible.locks_changed",
            row.id,
            {"version_no": row.version_no, "fields": row.locked_fields},
        )
    return read_version(row)
