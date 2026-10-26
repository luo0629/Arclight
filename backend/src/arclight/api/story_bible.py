"""Narrative rule management, separate from the visual asset lifecycle."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select, update

from arclight.api.collaboration import audit, require_idle
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.story_context import build_story_context, validate_entry
from arclight.core.errors import DomainError
from arclight.domain.story_bible import StoryEntryInput
from arclight.persistence.models import Episode, Project, StoryEntry

router = APIRouter(prefix="/api/v1", tags=["story-bible"])


class StoryEntryRead(Resource, StoryEntryInput):
    project_id: UUID
    archived: bool
    revision: int
    created_by: str


class StoryEntryReplace(StoryEntryInput):
    expected_revision: int = Field(strict=True, ge=0)


class StoryArchive(Input):
    expected_revision: int = Field(strict=True, ge=0)
    archived: bool = Field(strict=True)


def read_entry(row: StoryEntry) -> StoryEntryRead:
    return StoryEntryRead(
        id=row.id,
        project_id=row.project_id,
        kind=StoryEntryInput.model_validate({"kind": row.kind, "name": row.name}).kind,
        name=row.name,
        archived=row.archived,
        revision=row.revision,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **row.data_json,
    )


def edit_project(db, project_id, actor):
    require_project(db, project_id, actor, write=True)
    db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    require_idle(db, select(Episode.id).where(Episode.project_id == project_id))


def find_entry(db, project_id, entry_id):
    row = db.get(StoryEntry, entry_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "故事设定不存在", 404)
    return row


@router.get("/projects/{project_id}/story-entries")
def list_entries(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[StoryEntryRead]:
    require_project(db, project_id, actor)
    rows = db.scalars(
        select(StoryEntry)
        .where(StoryEntry.project_id == project_id)
        .order_by(StoryEntry.created_at, StoryEntry.id)
        .limit(limit)
        .offset(offset)
    )
    return [read_entry(row) for row in rows]


@router.get("/projects/{project_id}/story-entries/{entry_id}")
def get_entry(project_id: UUID, entry_id: UUID, db: Database, actor: Actor) -> StoryEntryRead:
    require_project(db, project_id, actor)
    return read_entry(find_entry(db, project_id, entry_id))


@router.post("/projects/{project_id}/story-entries", status_code=201)
def create_entry(
    project_id: UUID, payload: StoryEntryInput, db: Database, actor: Actor
) -> StoryEntryRead:
    edit_project(db, project_id, actor)
    validate_entry(db, project_id, payload)
    row = StoryEntry(
        project_id=project_id,
        kind=payload.kind,
        name=payload.name,
        created_by=actor,
        data_json=payload.model_dump(mode="json", exclude={"kind", "name"}),
    )
    db.add(row)
    db.flush()
    build_story_context(db, project_id, validate_assets=False)
    audit(db, project_id, actor, "story_entry.created", row.id, {"kind": row.kind, "revision": 0})
    return read_entry(row)


@router.put("/projects/{project_id}/story-entries/{entry_id}")
def replace_entry(
    project_id: UUID, entry_id: UUID, payload: StoryEntryReplace, db: Database, actor: Actor
) -> StoryEntryRead:
    edit_project(db, project_id, actor)
    row = find_entry(db, project_id, entry_id)
    if row.archived:
        raise DomainError("STORY_ENTRY_ARCHIVED", "请先恢复故事设定再编辑", 409)
    if row.kind != payload.kind:
        raise DomainError("STORY_KIND_IMMUTABLE", "设定类型不可更改，请新建设定", 422)
    validate_entry(db, project_id, payload, entry_id)
    changed = db.scalar(
        update(StoryEntry)
        .where(StoryEntry.id == entry_id, StoryEntry.revision == payload.expected_revision)
        .values(
            name=payload.name,
            data_json=payload.model_dump(
                mode="json", exclude={"kind", "name", "expected_revision"}
            ),
            revision=StoryEntry.revision + 1,
        )
        .returning(StoryEntry.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "设定已被修改，请保留草稿并重新载入", 409)
    db.refresh(row)
    build_story_context(db, project_id, validate_assets=False)
    audit(db, project_id, actor, "story_entry.updated", row.id, {"revision": row.revision})
    return read_entry(row)


@router.patch("/projects/{project_id}/story-entries/{entry_id}")
def archive_entry(
    project_id: UUID, entry_id: UUID, payload: StoryArchive, db: Database, actor: Actor
) -> StoryEntryRead:
    edit_project(db, project_id, actor)
    row = find_entry(db, project_id, entry_id)
    if payload.archived:
        others = db.scalars(
            select(StoryEntry).where(
                StoryEntry.project_id == project_id,
                StoryEntry.archived.is_(False),
                StoryEntry.id != entry_id,
            )
        )
        if any(
            str(entry_id) == link["target_id"]
            for other in others
            for link in other.data_json.get("relations", [])
        ):
            raise DomainError(
                "STORY_ENTRY_IN_USE", "仍有活动设定引用此项，请先解除关联或归档引用方", 409
            )
    else:
        validate_entry(
            db, project_id, StoryEntryInput(kind=row.kind, name=row.name, **row.data_json), entry_id
        )
    changed = db.scalar(
        update(StoryEntry)
        .where(StoryEntry.id == entry_id, StoryEntry.revision == payload.expected_revision)
        .values(archived=payload.archived, revision=StoryEntry.revision + 1)
        .returning(StoryEntry.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "设定已被修改，请重新载入", 409)
    db.refresh(row)
    build_story_context(db, project_id, validate_assets=False)
    audit(
        db,
        project_id,
        actor,
        "story_entry.archived" if row.archived else "story_entry.restored",
        row.id,
        {"revision": row.revision},
    )
    return read_entry(row)
