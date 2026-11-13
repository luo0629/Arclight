"""Role voice versions and explicit dialogue TTS production."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select, tuple_

from arclight.api.dependencies import Actor, Database
from arclight.application.audio_tts import (
    create_tts,
    read_result,
    read_tts,
    regenerate_tts,
    register_result,
    require_tts,
)
from arclight.application.audio_voices import create_voice_version, read_voice, require_voice
from arclight.application.commands import require_project
from arclight.domain.audio_tts import (
    AudioTTSCreate,
    AudioTTSRead,
    AudioTTSRegenerate,
    AudioTTSRegister,
    AudioTTSResultRead,
)
from arclight.domain.audio_voices import AudioVoiceCreate, AudioVoiceRead
from arclight.persistence.audio_voices import AudioVoiceVersion

router = APIRouter(tags=["audio-production"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.post("/projects/{project_id}/audio-voices", status_code=201)
def create_voice(
    project_id: UUID, payload: AudioVoiceCreate, response: Response, db: Database, actor: Actor
) -> AudioVoiceRead:
    row, created = create_voice_version(db, project_id, actor, payload)
    response.status_code = 201 if created else 200
    return read_voice(row)


@router.get("/projects/{project_id}/audio-voices")
def voices(
    project_id: UUID,
    db: Database,
    actor: Actor,
    speaker: Annotated[str | None, Query(max_length=100)] = None,
    latest_only: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[AudioVoiceRead]:
    require_project(db, project_id, actor)
    query = select(AudioVoiceVersion).where(AudioVoiceVersion.project_id == project_id)
    if speaker is not None:
        query = query.where(AudioVoiceVersion.speaker == speaker)
    if latest_only:
        query = query.where(
            tuple_(AudioVoiceVersion.speaker, AudioVoiceVersion.version_no).in_(
                select(AudioVoiceVersion.speaker, func.max(AudioVoiceVersion.version_no))
                .where(AudioVoiceVersion.project_id == project_id)
                .group_by(AudioVoiceVersion.speaker)
            )
        )
    return [
        read_voice(row)
        for row in db.scalars(
            query.order_by(AudioVoiceVersion.speaker, AudioVoiceVersion.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/projects/{project_id}/audio-voices/{voice_version_id}")
def get_voice(
    project_id: UUID, voice_version_id: UUID, db: Database, actor: Actor
) -> AudioVoiceRead:
    return read_voice(require_voice(db, project_id, voice_version_id, actor))


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/tts", status_code=202)
def create(
    snapshot_id: UUID,
    line_id: str,
    payload: AudioTTSCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> AudioTTSRead:
    row, created = create_tts(
        db,
        snapshot_id,
        line_id,
        actor,
        payload,
        execution_policy=request.app.state.settings.media_execution_policy,
    )
    response.status_code = 202 if created else 200
    return row


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/tts/regenerate", status_code=202)
def regenerate(
    snapshot_id: UUID,
    line_id: str,
    payload: AudioTTSRegenerate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> AudioTTSRead:
    row, created = regenerate_tts(
        db,
        snapshot_id,
        line_id,
        actor,
        payload,
        execution_policy=request.app.state.settings.media_execution_policy,
    )
    response.status_code = 202 if created else 200
    return row


@router.get("/audio-snapshots/{snapshot_id}/lines/{line_id}/tts/{job_id}")
def get(snapshot_id: UUID, line_id: str, job_id: UUID, db: Database, actor: Actor) -> AudioTTSRead:
    return read_tts(db, require_tts(db, snapshot_id, line_id, job_id, actor))


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/tts/{job_id}/result", status_code=201)
def register(
    snapshot_id: UUID,
    line_id: str,
    job_id: UUID,
    payload: AudioTTSRegister,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> AudioTTSResultRead:
    row = require_tts(db, snapshot_id, line_id, job_id, actor, write=True)
    result, created = register_result(db, request.app.state.settings, row, actor, payload)
    response.status_code = 201 if created else 200
    return result


@router.get("/audio-snapshots/{snapshot_id}/lines/{line_id}/tts/{job_id}/result")
def result(
    snapshot_id: UUID, line_id: str, job_id: UUID, db: Database, actor: Actor
) -> AudioTTSResultRead:
    return read_result(db, require_tts(db, snapshot_id, line_id, job_id, actor))
