"""Authorized immutable manifest capture/history; result writes belong to trusted services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, or_, select

from arclight.api.dependencies import Actor, Database
from arclight.application.render_manifests import create_render_manifest, read_render_manifest
from arclight.application.render_results import read_render_results
from arclight.application.timeline_store import read_timeline
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import (
    RenderManifestCreate,
    RenderManifestRead,
    RenderManifestResults,
)
from arclight.persistence.render_manifests import RenderManifest

router = APIRouter(prefix="/api/v1", tags=["render-manifests"])


@router.post("/timelines/{timeline_id}/render-manifests", status_code=201)
def create(
    timeline_id: UUID, payload: RenderManifestCreate, request: Request, db: Database, actor: Actor
) -> RenderManifestRead:
    return create_render_manifest(db, request.app.state.settings, timeline_id, actor, payload)


@router.get("/timelines/{timeline_id}/render-manifests")
def history(
    timeline_id: UUID,
    db: Database,
    actor: Actor,
    before_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[RenderManifestRead]:
    read_timeline(db, timeline_id, actor)
    query = select(RenderManifest).where(RenderManifest.timeline_id == timeline_id)
    if before_id:
        before = db.get(RenderManifest, before_id)
        if before is None or before.timeline_id != timeline_id:
            raise DomainError("NOT_FOUND", "本时间轴的渲染清单游标不存在", 404)
        query = query.where(
            or_(
                RenderManifest.created_at < before.created_at,
                and_(RenderManifest.created_at == before.created_at, RenderManifest.id < before.id),
            )
        )
    rows = db.scalars(
        query.order_by(RenderManifest.created_at.desc(), RenderManifest.id.desc()).limit(limit)
    )
    return [read_render_manifest(db, row.id, actor) for row in rows]


@router.get("/render-manifests/{manifest_id}")
def get(manifest_id: UUID, db: Database, actor: Actor) -> RenderManifestRead:
    return read_render_manifest(db, manifest_id, actor)


@router.get("/render-manifests/{manifest_id}/results")
def results(manifest_id: UUID, db: Database, actor: Actor) -> RenderManifestResults:
    return read_render_results(db, manifest_id, actor)
