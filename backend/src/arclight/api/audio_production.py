"""Audio preparation, frozen dialogue reads and exact version registration."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.application.audio_import import import_audio, read_import
from arclight.application.audio_production import (
    bind_audio,
    capture_line_inputs,
    capture_snapshot,
    require_line,
    require_snapshot,
)
from arclight.application.commands import require_episode
from arclight.domain.audio_production import (
    AudioBindingCreate,
    AudioBindingRead,
    AudioImportCreate,
    AudioImportRead,
    AudioInputCreate,
    AudioInputRead,
    AudioLine,
    AudioSnapshotCreate,
    AudioSnapshotRead,
)
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot

router = APIRouter(prefix="/api/v1", tags=["audio-production"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.post("/episodes/{episode_id}/audio-snapshots", status_code=201)
def create(
    episode_id: UUID, payload: AudioSnapshotCreate, db: Database, actor: Actor
) -> AudioSnapshotRead:
    return AudioSnapshotRead.model_validate(capture_snapshot(db, episode_id, actor, payload))


@router.get("/episodes/{episode_id}/audio-snapshots")
def snapshots(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 100, offset: Offset = 0
) -> list[AudioSnapshotRead]:
    require_episode(db, episode_id, actor)
    rows = db.scalars(
        select(AudioSnapshot)
        .where(AudioSnapshot.episode_id == episode_id)
        .order_by(AudioSnapshot.created_at, AudioSnapshot.id)
        .offset(offset)
        .limit(limit)
    )
    return [AudioSnapshotRead.model_validate(row) for row in rows]


@router.get("/audio-snapshots/{snapshot_id}")
def get(snapshot_id: UUID, db: Database, actor: Actor) -> AudioSnapshotRead:
    return AudioSnapshotRead.model_validate(require_snapshot(db, snapshot_id, actor))


@router.get("/audio-snapshots/{snapshot_id}/lines")
def lines(
    snapshot_id: UUID, db: Database, actor: Actor, limit: Limit = 100, offset: Offset = 0
) -> list[AudioLine]:
    row = require_snapshot(db, snapshot_id, actor)
    return [AudioLine.model_validate(value) for value in row.lines_json[offset : offset + limit]]


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/inputs", status_code=201)
def inputs(
    snapshot_id: UUID, line_id: str, payload: AudioInputCreate, db: Database, actor: Actor
) -> AudioInputRead:
    row = capture_line_inputs(db, snapshot_id, line_id, actor, payload)
    return AudioInputRead(snapshot_id=snapshot_id, line_id=line_id, dependency_manifest_id=row.id)


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/bindings", status_code=201)
def bind(
    snapshot_id: UUID,
    line_id: str,
    payload: AudioBindingCreate,
    request: Request,
    db: Database,
    actor: Actor,
) -> AudioBindingRead:
    return AudioBindingRead.model_validate(
        bind_audio(
            db, request.app.state.settings, snapshot_id, line_id, payload.asset_version_id, actor
        )
    )


@router.get("/audio-snapshots/{snapshot_id}/lines/{line_id}/bindings")
def bindings(
    snapshot_id: UUID,
    line_id: str,
    db: Database,
    actor: Actor,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[AudioBindingRead]:
    row = require_snapshot(db, snapshot_id, actor)
    require_line(row, line_id)
    values = db.scalars(
        select(AudioLineBinding)
        .where(AudioLineBinding.snapshot_id == row.id, AudioLineBinding.line_id == line_id)
        .order_by(AudioLineBinding.created_at, AudioLineBinding.id)
        .offset(offset)
        .limit(limit)
    )
    return [AudioBindingRead.model_validate(value) for value in values]


@router.post("/audio-snapshots/{snapshot_id}/lines/{line_id}/imports", status_code=201)
def import_recording(
    snapshot_id: UUID,
    line_id: str,
    payload: AudioImportCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> AudioImportRead:
    result, created = import_audio(
        db, request.app.state.settings, snapshot_id, line_id, actor, payload
    )
    response.status_code = 201 if created else 200
    return result


@router.get("/audio-snapshots/{snapshot_id}/lines/{line_id}/imports/{version_id}")
def get_import(
    snapshot_id: UUID, line_id: str, version_id: UUID, db: Database, actor: Actor
) -> AudioImportRead:
    snapshot = require_snapshot(db, snapshot_id, actor)
    require_line(snapshot, line_id)
    return read_import(db, snapshot.id, line_id, version_id)
