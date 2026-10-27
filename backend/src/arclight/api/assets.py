"""Project visual asset directory; media versions and selection are independent."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.core.errors import DomainError
from arclight.domain.asset_metadata import AssetMetadata, AssetOrigin
from arclight.persistence.models import Episode, VisualAsset

router = APIRouter(prefix="/api/v1", tags=["visual-assets"])
AssetType = Literal["CHARACTER", "SCENE", "PROP", "STYLE"]
DIRECTORY_TYPES = ("CHARACTER", "SCENE", "PROP", "STYLE")


class AssetCreate(Input):
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=160)
    metadata: AssetMetadata = Field(default_factory=AssetMetadata)
    creation_source: AssetOrigin = Field(default_factory=AssetOrigin)


class AssetMetadataUpdate(Input):
    metadata: AssetMetadata
    expected_revision: int = Field(strict=True, ge=0)


class AssetRename(Input):
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(strict=True, ge=0)


class AssetArchive(Input):
    archived: bool = Field(strict=True)
    expected_revision: int = Field(strict=True, ge=0)


class AssetRead(Resource):
    project_id: UUID
    asset_type: AssetType
    name: str
    status: str
    current_version_id: UUID | None
    lock_version: int
    revision: int
    metadata: AssetMetadata = Field(validation_alias="metadata_json")
    creation_source: AssetOrigin | None
    created_by: str | None


def validate_scope(db: Session, project_id: UUID, metadata: AssetMetadata) -> None:
    for episode_id in metadata.usage_scope.episode_ids:
        episode = db.get(Episode, episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("INVALID_ASSET_SCOPE", "使用范围只能包含本项目剧集", 422)


def find_asset(db: Session, project_id: UUID, asset_id: UUID) -> VisualAsset:
    row = db.get(VisualAsset, asset_id, populate_existing=True)
    if row is None or row.project_id != project_id or row.asset_type not in DIRECTORY_TYPES:
        raise DomainError("NOT_FOUND", "视觉资产不存在", 404)
    return row


def change_asset(db: Session, row: VisualAsset, expected: int, **values) -> None:
    changed = db.scalar(
        update(VisualAsset)
        .where(VisualAsset.id == row.id, VisualAsset.revision == expected)
        .values(**values, revision=VisualAsset.revision + 1)
        .returning(VisualAsset.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "资产已被修改，请保留草稿并重新载入", 409)
    db.refresh(row)


@router.get("/projects/{project_id}/assets")
def list_assets(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[AssetRead]:
    require_project(db, project_id, actor)
    rows = db.scalars(
        select(VisualAsset)
        .where(VisualAsset.project_id == project_id, VisualAsset.asset_type.in_(DIRECTORY_TYPES))
        .order_by(VisualAsset.created_at, VisualAsset.id)
        .limit(limit)
        .offset(offset)
    )
    return [AssetRead.model_validate(row) for row in rows]


@router.get("/projects/{project_id}/assets/{asset_id}")
def get_asset(project_id: UUID, asset_id: UUID, db: Database, actor: Actor) -> AssetRead:
    require_project(db, project_id, actor)
    return AssetRead.model_validate(find_asset(db, project_id, asset_id))


@router.post("/projects/{project_id}/assets", status_code=201)
def create_asset(project_id: UUID, payload: AssetCreate, db: Database, actor: Actor) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    validate_scope(db, project_id, payload.metadata)
    row = VisualAsset(
        project_id=project_id,
        asset_type=payload.asset_type,
        name=payload.name,
        metadata_json=payload.metadata.model_dump(mode="json"),
        creation_source=payload.creation_source.model_dump(mode="json"),
        created_by=actor,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit(db, project_id, actor, "asset.created", row.id, {"asset_type": row.asset_type})
    return AssetRead.model_validate(row)


@router.put("/projects/{project_id}/assets/{asset_id}")
def rename_asset(
    project_id: UUID, asset_id: UUID, payload: AssetRename, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = find_asset(db, project_id, asset_id)
    if row.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定的资产不可编辑", 409)
    change_asset(db, row, payload.expected_revision, name=payload.name)
    audit(db, project_id, actor, "asset.updated", row.id, {"revision": row.revision})
    return AssetRead.model_validate(row)


@router.put("/projects/{project_id}/assets/{asset_id}/metadata")
def update_metadata(
    project_id: UUID, asset_id: UUID, payload: AssetMetadataUpdate, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = find_asset(db, project_id, asset_id)
    if row.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定的资产不可编辑", 409)
    validate_scope(db, project_id, payload.metadata)
    change_asset(
        db, row, payload.expected_revision, metadata_json=payload.metadata.model_dump(mode="json")
    )
    audit(db, project_id, actor, "asset.metadata_updated", row.id, {"revision": row.revision})
    return AssetRead.model_validate(row)


@router.patch("/projects/{project_id}/assets/{asset_id}")
def archive_asset(
    project_id: UUID, asset_id: UUID, payload: AssetArchive, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = find_asset(db, project_id, asset_id)
    if row.status == "LOCKED":
        raise DomainError("ASSET_READ_ONLY", "锁定的资产不可归档", 409)
    if payload.archived == (row.status == "ARCHIVED"):
        raise DomainError("ASSET_STATE_CONFLICT", "资产归档状态已改变，请重新载入", 409)
    change_asset(
        db,
        row,
        payload.expected_revision,
        status="ARCHIVED" if payload.archived else (row.archived_from_status or "DRAFT"),
        archived_from_status=row.status if payload.archived else None,
    )
    audit(
        db,
        project_id,
        actor,
        "asset.archived" if payload.archived else "asset.restored",
        row.id,
        {"revision": row.revision},
    )
    return AssetRead.model_validate(row)
