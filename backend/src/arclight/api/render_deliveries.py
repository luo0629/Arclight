"""Authorized historical films; S3 downloads use existing revocable file tickets."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from arclight.api.dependencies import Actor, Database
from arclight.api.human_reviews import HumanReviewRead, serialize
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.render_deliveries import (
    delivery_row,
    list_deliveries,
    read_delivery,
    read_delivery_report,
)
from arclight.application.render_inputs import file_storage
from arclight.application.render_reports import request_delivery_warning_review
from arclight.application.render_results import MAX_RENDER_BYTES
from arclight.core.errors import DomainError
from arclight.domain.render_deliveries import RenderDeliveryDetail, RenderDeliveryRead
from arclight.domain.render_reports import RenderDeliveryReport
from arclight.persistence.models import StoredFile

router = APIRouter(prefix="/api/v1/episodes/{episode_id}/deliveries", tags=["render-deliveries"])


@router.get("")
def history(
    episode_id: UUID,
    response: Response,
    db: Database,
    actor: Actor,
    before_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[RenderDeliveryRead]:
    response.headers["Cache-Control"] = "no-store"
    return list_deliveries(db, episode_id, actor, before_id, limit)


@router.get("/{artifact_id}")
def get(
    episode_id: UUID, artifact_id: UUID, response: Response, db: Database, actor: Actor
) -> RenderDeliveryDetail:
    response.headers["Cache-Control"] = "no-store"
    return read_delivery(db, episode_id, artifact_id, actor)


@router.get("/{artifact_id}/content")
def content(
    episode_id: UUID, artifact_id: UUID, request: Request, db: Database, actor: Actor
) -> Response:
    episode = require_episode(db, episode_id, actor)
    row = delivery_row(db, episode_id, artifact_id)
    file = db.get(StoredFile, row.file_id)
    assert file is not None
    if file.storage_backend != "LOCAL":
        raise DomainError("PRESIGNED_REQUIRED", "请通过项目文件下载凭证接口获取短期地址", 409)
    if (
        file.project_id != episode.project_id
        or file.asset_version_id != row.asset_version_id
        or file.checksum != row.checksum
        or file.byte_size != row.byte_size
        or not file.rights_confirmed
    ):
        raise DomainError("RENDER_HISTORY_INVALID", "成片文件与精确回执不匹配", 409)
    data = file_storage(request.app.state.settings, file).read(
        file.object_key, MAX_RENDER_BYTES, file.checksum
    )
    if len(data) != row.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "成片文件大小与回执不匹配", 409)
    audit(db, episode.project_id, actor, "render.downloaded", row.id, {"file_id": str(file.id)})
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="render-{row.id}.mp4"',
        },
    )


@router.get("/{artifact_id}/report")
def report(
    episode_id: UUID, artifact_id: UUID, response: Response, db: Database, actor: Actor
) -> RenderDeliveryReport:
    response.headers["Cache-Control"] = "no-store"
    return read_delivery_report(db, episode_id, artifact_id, actor)


@router.post("/{artifact_id}/warning-review")
def warning_review(
    episode_id: UUID, artifact_id: UUID, db: Database, actor: Actor
) -> HumanReviewRead:
    require_episode(db, episode_id, actor, write=True, owner=True)
    artifact = delivery_row(db, episode_id, artifact_id)
    request = request_delivery_warning_review(db, artifact, actor)
    return serialize(db, request)
