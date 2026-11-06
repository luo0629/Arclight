from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select

from arclight.agents.story_outline import StoryOutlineContent
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import AcceptedRun, Input, Resource
from arclight.application.commands import require_episode
from arclight.application.story_outline import (
    confirm_outline,
    require_outline,
    save_outline,
    start_outline,
)
from arclight.domain.screenwriter_creation import ScreenwriterOptions
from arclight.persistence.models import StoryOutline

router = APIRouter(prefix="/api/v1/episodes/{episode_id}", tags=["story-outlines"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class OutlineCreate(Input, ScreenwriterOptions):
    story: Annotated[str, StringConstraints(strip_whitespace=False)] | None = Field(
        default=None, min_length=1, max_length=20000
    )
    source_id: UUID | None = None
    analysis_id: UUID | None = None
    target_episode_count: int | None = Field(default=None, strict=True, ge=1, le=1000)
    episode_duration_ms: int | None = Field(default=None, strict=True, ge=2500, le=1800000)

    @model_validator(mode="after")
    def one_source(self) -> "OutlineCreate":
        if sum(value is not None for value in (self.story, self.source_id, self.analysis_id)) != 1:
            raise ValueError("请提供故事文本、精确来源或精确提炼版本，三选一")
        return self


class OutlineSave(Input):
    base_version_id: UUID
    content: StoryOutlineContent


class OutlineRead(Resource):
    project_id: UUID
    episode_id: UUID
    run_id: UUID | None
    base_version_id: UUID | None
    version_no: int
    status: str
    created_by: str
    confirmed_by: str | None
    confirmed_at: datetime | None

    @field_validator("created_at", "updated_at", "confirmed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class OutlineDetail(OutlineRead):
    content: StoryOutlineContent
    source_snapshot: dict


def detail(row: StoryOutline) -> OutlineDetail:
    return OutlineDetail(
        **OutlineRead.model_validate(row).model_dump(),
        content=StoryOutlineContent.model_validate(row.content_json),
        source_snapshot=row.source_snapshot,
    )


@router.post("/story-outline-runs", status_code=202)
def create_outline(
    episode_id: UUID,
    payload: OutlineCreate,
    db: Database,
    actor: Actor,
    request: Request,
    idempotency_key: Key,
) -> AcceptedRun:
    run = start_outline(
        db,
        episode_id,
        actor,
        payload.model_dump(mode="json", exclude={"professional", "agent_limits"})
        | (
            {"professional": True, "agent_limits": payload.frozen_limits().model_dump(mode="json")}
            if payload.professional
            else {}
        ),
        idempotency_key,
        request.state.request_id,
        request.app.state.settings,
    )
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.get("/story-outlines")
def list_outlines(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[OutlineRead]:
    episode = require_episode(db, episode_id, actor)
    return [
        OutlineRead.model_validate(row)
        for row in db.scalars(
            select(StoryOutline)
            .where(StoryOutline.project_id == episode.project_id)
            .order_by(StoryOutline.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/story-outlines/{version_id}")
def get_outline(episode_id: UUID, version_id: UUID, db: Database, actor: Actor) -> OutlineDetail:
    episode = require_episode(db, episode_id, actor)
    return detail(require_outline(db, episode.project_id, version_id))


@router.post("/story-outlines", status_code=201)
def edit_outline(
    episode_id: UUID, payload: OutlineSave, db: Database, actor: Actor, idempotency_key: Key
) -> OutlineDetail:
    return detail(
        save_outline(
            db, episode_id, actor, payload.base_version_id, payload.content, idempotency_key
        )
    )


@router.post("/story-outlines/{version_id}/confirm")
def approve_outline(
    episode_id: UUID, version_id: UUID, db: Database, actor: Actor
) -> OutlineDetail:
    return detail(confirm_outline(db, episode_id, actor, version_id))
