"""Immutable, episode-scoped sources with authorized private-object reads."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import or_, select

from arclight.api.dependencies import Actor, Database
from arclight.api.rights import UsageDeclaration
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Resource
from arclight.application.commands import require_episode
from arclight.application.source_objects import source_original, source_text
from arclight.application.story_sources import (
    SourceDraft,
    decode_inline_text,
    provenance,
    revision_snapshot,
    save_source,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.story_import import MAX_BASE64_LENGTH, decode_source
from arclight.persistence.models import (
    Episode,
    Script,
    StoryAnalysis,
    StoryOutline,
    StorySource,
    StorySourceBinding,
    StorySourceRevision,
    WorkflowRun,
)

router = APIRouter(prefix="/api/v1/episodes/{episode_id}/story-sources", tags=["story-sources"])


class SourceImport(UsageDeclaration):
    import_key: UUID
    original_name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(max_length=64)
    encoding: Literal["utf-8", "gb18030"] = "utf-8"
    content_base64: str = Field(max_length=MAX_BASE64_LENGTH + 4)
    base_source_id: UUID | None = None
    change_note: str = Field(default="", max_length=1000)


class SourceText(SourceImport):
    # Reuse rights and revision validation, while file data is not accepted.
    content_base64: Literal[""] = ""
    original_name: str = Field(default="故事.txt", min_length=1, max_length=160)
    media_type: Literal["text/plain"] = "text/plain"
    encoding: Literal["utf-8"] = "utf-8"
    text: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=262144
    )


class ObjectReference(BaseModel):
    backend: Literal["database", "local", "s3"]
    key: str
    sha256: str
    byte_size: int


class SourceProvenance(BaseModel):
    source_id: UUID
    root_source_id: UUID
    parent_source_id: UUID | None
    version_no: int
    change_note: str
    origin_kind: Literal["FILE", "INLINE", "TEXT"]
    rights_status: Literal["CONFIRMED", "UNDECLARED"]
    source_note: str
    rights_note: str
    encoding: str
    sha256: str
    object_ref: ObjectReference


class SourceMetadata(Resource):
    episode_id: UUID
    import_no: int
    original_name: str
    source_type: str
    media_type: str
    encoding: str
    byte_size: int
    sha256: str
    source_note: str
    rights_note: str
    created_by: str

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        # SQLite drops timezone metadata; persisted timestamps represent UTC.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SourceRead(SourceMetadata):
    provenance: SourceProvenance


class SourceDetail(SourceRead):
    text: str


def source_read(row: StorySource, revision: StorySourceRevision | None) -> SourceRead:
    return SourceRead(
        **SourceMetadata.model_validate(row).model_dump(),
        provenance=SourceProvenance.model_validate(revision_snapshot(row, revision)),
    )


def source_detail(db: Database, settings: Settings, row: StorySource) -> SourceDetail:
    return SourceDetail(
        **SourceMetadata.model_validate(row).model_dump(),
        provenance=SourceProvenance.model_validate(provenance(db, row)),
        text=source_text(db, settings, row),
    )


def require_source(db: Database, episode_id: UUID, source_id: UUID, actor: str) -> StorySource:
    require_episode(db, episode_id, actor)
    row = db.get(StorySource, source_id)
    if row is None or row.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "故事来源不存在", 404)
    return row


@router.get("")
def list_sources(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[SourceRead]:
    require_episode(db, episode_id, actor)
    return [
        source_read(row, revision)
        for row, revision in db.execute(
            select(StorySource, StorySourceRevision)
            .outerjoin(StorySourceRevision, StorySourceRevision.source_id == StorySource.id)
            .where(StorySource.episode_id == episode_id)
            .order_by(StorySource.import_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.post("", status_code=201)
def import_source(
    episode_id: UUID, payload: SourceImport, request: Request, db: Database, actor: Actor
) -> SourceDetail:
    episode = require_episode(db, episode_id, actor, write=True)
    # The episode may have been cached before waiting for the project write gate.
    db.refresh(episode)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    decoded = decode_source(
        payload.original_name, payload.media_type, payload.encoding, payload.content_base64
    )
    values = payload.model_dump(mode="json", exclude={"content_base64", "import_key"})
    if payload.base_source_id is None and not payload.change_note:
        # Preserve pre-0011 file-import idempotency fingerprints.
        values.pop("base_source_id")
        values.pop("change_note")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                **values,
                "sha256": decoded.sha256,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    row = save_source(
        db,
        episode,
        actor,
        SourceDraft(
            payload.original_name,
            payload.media_type,
            decoded,
            payload.source_note,
            payload.rights_note,
            base_source_id=payload.base_source_id,
            change_note=payload.change_note,
        ),
        payload.import_key,
        fingerprint,
        settings=request.app.state.settings,
    )
    return source_detail(db, request.app.state.settings, row)


@router.post("/text", status_code=201)
def save_text_source(
    episode_id: UUID, payload: SourceText, request: Request, db: Database, actor: Actor
) -> SourceDetail:
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    decoded = decode_inline_text(payload.text, payload.original_name)
    fingerprint = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    row = save_source(
        db,
        episode,
        actor,
        SourceDraft(
            payload.original_name,
            "text/plain",
            decoded,
            payload.source_note,
            payload.rights_note,
            origin_kind="TEXT",
            base_source_id=payload.base_source_id,
            change_note=payload.change_note,
        ),
        payload.import_key,
        fingerprint,
        settings=request.app.state.settings,
    )
    return source_detail(db, request.app.state.settings, row)


@router.get("/{source_id}/history")
def source_history(
    episode_id: UUID,
    source_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[SourceRead]:
    row = require_source(db, episode_id, source_id, actor)
    root = UUID(provenance(db, row)["root_source_id"])
    return [
        source_read(source, revision)
        for source, revision in db.execute(
            select(StorySource, StorySourceRevision)
            .join(StorySourceRevision, StorySourceRevision.source_id == StorySource.id)
            .where(StorySource.episode_id == episode_id, StorySourceRevision.root_source_id == root)
            .order_by(StorySourceRevision.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


class SourceUsage(BaseModel):
    id: UUID
    run_id: UUID
    episode_id: UUID
    workflow_type: str
    status: str
    analysis_id: UUID | None
    outline_id: UUID | None
    script_id: UUID | None


@router.get("/{source_id}/usages")
def source_usages(
    episode_id: UUID,
    source_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[SourceUsage]:
    require_source(db, episode_id, source_id, actor)
    episode = require_episode(db, episode_id, actor)
    rows = db.execute(
        select(
            WorkflowRun.id.label("id"),
            WorkflowRun.id.label("run_id"),
            WorkflowRun.episode_id,
            WorkflowRun.workflow_type,
            WorkflowRun.status,
            StoryAnalysis.id.label("analysis_id"),
            StoryOutline.id.label("outline_id"),
            Script.id.label("script_id"),
        )
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .outerjoin(StorySourceBinding, StorySourceBinding.run_id == WorkflowRun.id)
        .outerjoin(StoryAnalysis, StoryAnalysis.run_id == WorkflowRun.id)
        .outerjoin(StoryOutline, StoryOutline.run_id == WorkflowRun.id)
        .outerjoin(Script, Script.run_id == WorkflowRun.id)
        .where(
            Episode.project_id == episode.project_id,
            or_(
                StorySourceBinding.source_id == source_id,
                StoryAnalysis.source_id == source_id,
                WorkflowRun.input_json["source_id"].as_string() == str(source_id),
                WorkflowRun.input_json["source_snapshot"]["source_id"].as_string()
                == str(source_id),
                WorkflowRun.input_json["outline_context"]["source_snapshot"][
                    "source_id"
                ].as_string()
                == str(source_id),
            ),
        )
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).mappings()
    return [SourceUsage.model_validate(row) for row in rows]


@router.get("/{source_id}")
def get_source(
    episode_id: UUID, source_id: UUID, request: Request, db: Database, actor: Actor
) -> SourceDetail:
    return source_detail(
        db, request.app.state.settings, require_source(db, episode_id, source_id, actor)
    )


@router.get("/{source_id}/original")
def original_source(
    episode_id: UUID, source_id: UUID, request: Request, db: Database, actor: Actor
) -> Response:
    row = require_source(db, episode_id, source_id, actor)
    return Response(
        source_original(db, request.app.state.settings, row),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(row.original_name, safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
