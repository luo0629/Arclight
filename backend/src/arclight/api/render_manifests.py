"""Authorized immutable manifest capture/history; result writes belong to trusted services."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import and_, or_, select

from arclight.api.dependencies import Actor, Database, ShortSessionActor
from arclight.api.human_reviews import HumanReviewRead, serialize
from arclight.application.formal_export import apply_formal_export, prepare_formal_export
from arclight.application.human_reviews import require_review
from arclight.application.render_admission import admit_render
from arclight.application.render_manifests import (
    create_render_manifest,
    manifest_episode_for_write,
    read_render_manifest,
)
from arclight.application.render_results import read_render_results
from arclight.application.render_reviews import formal_export_required
from arclight.application.timeline_store import read_timeline
from arclight.core.errors import DomainError
from arclight.domain.render_jobs import RenderJobRead
from arclight.domain.render_manifests import (
    FormalExportCommand,
    RenderAdmissionRead,
    RenderContract,
    RenderManifestCreate,
    RenderManifestRead,
    RenderManifestResults,
    RenderManifestSettings,
)
from arclight.persistence.render_manifests import RenderManifest

router = APIRouter(prefix="/api/v1", tags=["render-manifests"])


class FormalExportPreparation(RenderContract):
    manifest: RenderManifestRead
    review: HumanReviewRead


class FormalExportProgress(RenderContract):
    review: HumanReviewRead
    job: RenderJobRead | None


@router.post("/timelines/{timeline_id}/render-manifests", status_code=201)
def create(
    timeline_id: UUID, payload: RenderManifestCreate, request: Request, db: Database, actor: Actor
) -> RenderManifestRead:
    if payload.profile == "FORMAL":
        manifest_episode_for_write(db, timeline_id, actor)
        raise formal_export_required()
    return create_render_manifest(db, request.app.state.settings, timeline_id, actor, payload)


@router.post("/timelines/{timeline_id}/formal-export", status_code=201)
def prepare_export(
    timeline_id: UUID,
    payload: RenderManifestSettings,
    request: Request,
    response: Response,
    actor: ShortSessionActor,
) -> FormalExportPreparation:
    response.headers["Cache-Control"] = "no-store"
    manifest, review_id = prepare_formal_export(
        request.app.state.session_factory, request.app.state.settings, timeline_id, actor, payload
    )
    with request.app.state.session_factory() as db:
        return FormalExportPreparation(
            manifest=manifest, review=serialize(db, require_review(db, review_id, actor))
        )


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


@router.post("/render-manifests/{manifest_id}/formal-export")
def export_command(
    manifest_id: UUID,
    payload: FormalExportCommand,
    request: Request,
    response: Response,
    actor: ShortSessionActor,
) -> FormalExportProgress:
    response.headers["Cache-Control"] = "no-store"
    review_id, job = apply_formal_export(
        request.app.state.session_factory, request.app.state.settings, manifest_id, actor, payload
    )
    with request.app.state.session_factory() as db:
        return FormalExportProgress(
            review=serialize(db, require_review(db, review_id, actor)), job=job
        )


@router.post("/render-manifests/{manifest_id}/admission")
def admission(
    manifest_id: UUID, request: Request, db: Database, actor: Actor
) -> RenderAdmissionRead:
    return admit_render(db, request.app.state.settings, manifest_id, actor)
