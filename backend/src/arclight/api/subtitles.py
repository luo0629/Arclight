"""Permission-checked subtitle history and private exact SRT exports."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.application.subtitles import (
    edit_subtitles,
    generate_subtitles,
    read_subtitle,
    subtitle_document,
    subtitle_history,
)
from arclight.core.errors import DomainError
from arclight.domain.subtitles import (
    SubtitleEdit,
    SubtitleGenerate,
    SubtitleTrackRead,
    srt_bytes,
)
from arclight.persistence.subtitles import SubtitleProduction

router = APIRouter(prefix="/api/v1", tags=["subtitles"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.post("/episodes/{episode_id}/subtitle-tracks", status_code=201)
def create(
    episode_id: UUID, payload: SubtitleGenerate, request: Request, db: Database, actor: Actor
) -> SubtitleTrackRead:
    return generate_subtitles(db, request.app.state.settings, episode_id, payload, actor)


@router.post("/subtitle-tracks/{asset_version_id}/edits", status_code=201)
def edit(
    asset_version_id: UUID, payload: SubtitleEdit, request: Request, db: Database, actor: Actor
) -> SubtitleTrackRead:
    return edit_subtitles(db, request.app.state.settings, asset_version_id, payload, actor)


@router.get("/episodes/{episode_id}/subtitle-tracks")
def history(
    episode_id: UUID,
    request: Request,
    db: Database,
    actor: Actor,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[SubtitleTrackRead]:
    return subtitle_history(db, request.app.state.settings, episode_id, actor, limit, offset)


@router.get("/subtitle-tracks/{asset_version_id}")
def get(asset_version_id: UUID, request: Request, db: Database, actor: Actor) -> SubtitleTrackRead:
    return read_subtitle(db, request.app.state.settings, asset_version_id, actor)


@router.get("/subtitle-tracks/{asset_version_id}/srt")
def export(asset_version_id: UUID, request: Request, db: Database, actor: Actor) -> Response:
    read_subtitle(db, request.app.state.settings, asset_version_id, actor)
    row = db.scalar(
        select(SubtitleProduction).where(SubtitleProduction.asset_version_id == asset_version_id)
    )
    if row is None:
        raise DomainError("NOT_FOUND", "字幕生产版本不存在", 404)
    document = subtitle_document(db, row)
    return Response(
        srt_bytes(document),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="subtitles-{asset_version_id}.srt"',
            "Cache-Control": "private, no-store",
        },
    )
