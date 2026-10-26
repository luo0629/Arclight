"""Immutable, episode-scoped text sources. No external storage or provider calls."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import Field, field_validator
from sqlalchemy import func, select

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_episode
from arclight.core.errors import DomainError
from arclight.domain.story_import import MAX_BASE64_LENGTH, decode_source
from arclight.persistence.models import StorySource

router = APIRouter(prefix="/api/v1/episodes/{episode_id}/story-sources", tags=["story-sources"])


class SourceImport(Input):
    import_key: UUID
    original_name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(max_length=64)
    encoding: Literal["utf-8", "gb18030"] = "utf-8"
    content_base64: str = Field(max_length=MAX_BASE64_LENGTH + 4)
    source_note: str = Field(min_length=1, max_length=1000)
    rights_note: str = Field(min_length=1, max_length=1000)
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def confirm_rights(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认故事使用权")
        return value


class SourceRead(Resource):
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


class SourceDetail(SourceRead):
    text: str


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
        SourceRead.model_validate(row)
        for row in db.scalars(
            select(StorySource)
            .where(StorySource.episode_id == episode_id)
            .order_by(StorySource.import_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.post("", status_code=201)
def import_source(
    episode_id: UUID, payload: SourceImport, db: Database, actor: Actor
) -> SourceDetail:
    episode = require_episode(db, episode_id, actor, write=True)
    # The episode may have been cached before waiting for the project write gate.
    db.refresh(episode)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    decoded = decode_source(
        payload.original_name, payload.media_type, payload.encoding, payload.content_base64
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                **payload.model_dump(mode="json", exclude={"content_base64", "import_key"}),
                "sha256": decoded.sha256,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # require_episode acquired the project write gate, also serializing concurrent imports.
    existing = db.scalar(
        select(StorySource).where(
            StorySource.episode_id == episode_id, StorySource.import_key == payload.import_key
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一导入凭证不能用于不同文件或说明", 409)
        return SourceDetail.model_validate(existing)
    number = (
        db.scalar(
            select(func.max(StorySource.import_no)).where(StorySource.episode_id == episode_id)
        )
        or 0
    ) + 1
    row = StorySource(
        episode_id=episode_id,
        import_no=number,
        import_key=payload.import_key,
        request_fingerprint=fingerprint,
        original_name=payload.original_name,
        source_type=decoded.source_type,
        media_type=payload.media_type,
        encoding=decoded.encoding,
        byte_size=len(decoded.raw),
        sha256=decoded.sha256,
        raw_bytes=decoded.raw,
        text=decoded.text,
        source_note=payload.source_note,
        rights_note=payload.rights_note,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        episode.project_id,
        actor,
        "story_source.imported",
        row.id,
        {
            "episode_id": str(episode_id),
            "source_id": str(row.id),
            "sha256": row.sha256,
            "byte_size": row.byte_size,
            "rights_confirmed": True,
        },
    )
    return SourceDetail.model_validate(row)


@router.get("/{source_id}")
def get_source(episode_id: UUID, source_id: UUID, db: Database, actor: Actor) -> SourceDetail:
    return SourceDetail.model_validate(require_source(db, episode_id, source_id, actor))


@router.get("/{source_id}/original")
def original_source(episode_id: UUID, source_id: UUID, db: Database, actor: Actor) -> Response:
    row = require_source(db, episode_id, source_id, actor)
    return Response(
        row.raw_bytes,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(row.original_name, safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
