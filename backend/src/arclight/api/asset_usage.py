"""Authorized, paginated reverse lookup of actual content references."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from arclight.api.assets import find_asset
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.application.asset_usage import actual_usage
from arclight.application.commands import require_project

router = APIRouter(prefix="/api/v1", tags=["visual-assets"])


class AssetUsageRead(BaseModel):
    id: str
    asset_version_id: UUID
    asset_version_no: int
    episode_id: UUID
    episode_no: int
    episode_title: str
    episode_status: str
    content_kind: Literal["script", "storyboard"]
    content_id: UUID
    content_version_no: int
    content_status: str
    reference_path: str
    scene_id: UUID | None = None
    scene_no: int | None = None
    scene_title: str | None = None
    shot_no: int | None = None


@router.get("/projects/{project_id}/assets/{asset_id}/usage")
def list_asset_usage(
    project_id: UUID,
    asset_id: UUID,
    db: Database,
    actor: Actor,
    version_id: UUID | None = None,
    episode_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[AssetUsageRead]:
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id)
    return [
        AssetUsageRead.model_validate(row)
        for row in actual_usage(db, project_id, asset_id, version_id, episode_id, limit, offset)
    ]
