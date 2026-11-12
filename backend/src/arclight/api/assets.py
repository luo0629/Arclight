"""Project visual asset directory; media versions and selection are independent."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.asset_mutations import change_asset as change_asset
from arclight.application.asset_profiles import (
    validate_character_profile,
    validate_prop_profile,
    validate_scene_profile,
)
from arclight.application.commands import require_project
from arclight.core.errors import DomainError
from arclight.domain.asset_metadata import AssetMetadata, AssetOrigin
from arclight.domain.asset_profiles import CharacterProfile, PropProfile, SceneProfile
from arclight.persistence.models import AssetVersion, Episode, VisualAsset

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


class CharacterProfileUpdate(Input):
    profile: CharacterProfile
    expected_revision: int = Field(strict=True, ge=0)


class SceneProfileUpdate(Input):
    profile: SceneProfile
    expected_revision: int = Field(strict=True, ge=0)


class PropProfileUpdate(Input):
    profile: PropProfile
    expected_revision: int = Field(strict=True, ge=0)


class ReferenceImageRead(Resource):
    asset_id: UUID
    asset_name: str
    asset_type: str
    version_no: int
    status: str
    stale: bool


class AssetRename(Input):
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(strict=True, ge=0)


class AssetArchive(Input):
    archived: bool = Field(strict=True)
    expected_revision: int = Field(strict=True, ge=0)


class AssetRead(Resource):
    project_id: UUID
    asset_type: AssetType | Literal["IMAGE", "SHOT_IMAGE"]
    name: str
    status: str
    current_version_id: UUID | None
    lock_version: int
    revision: int
    metadata: AssetMetadata = Field(validation_alias="metadata_json")
    creation_source: AssetOrigin | None
    created_by: str | None
    character_profile: CharacterProfile | None
    scene_profile: SceneProfile | None
    prop_profile: PropProfile | None


def validate_scope(db: Session, project_id: UUID, metadata: AssetMetadata) -> None:
    for episode_id in metadata.usage_scope.episode_ids:
        episode = db.get(Episode, episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("INVALID_ASSET_SCOPE", "使用范围只能包含本项目剧集", 422)


def find_asset(
    db: Session, project_id: UUID, asset_id: UUID, *, include_media: bool = False
) -> VisualAsset:
    row = db.get(VisualAsset, asset_id, populate_existing=True)
    kinds = (*DIRECTORY_TYPES, "IMAGE", "SHOT_IMAGE") if include_media else DIRECTORY_TYPES
    if row is None or row.project_id != project_id or row.asset_type not in kinds:
        raise DomainError("NOT_FOUND", "视觉资产不存在", 404)
    return row


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
    return AssetRead.model_validate(find_asset(db, project_id, asset_id, include_media=True))


@router.get("/projects/{project_id}/reference-images")
def list_reference_images(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[ReferenceImageRead]:
    require_project(db, project_id, actor)
    rows = db.execute(
        select(AssetVersion, VisualAsset)
        .join(VisualAsset, AssetVersion.asset_id == VisualAsset.id)
        .where(
            VisualAsset.project_id == project_id,
            VisualAsset.asset_type.in_(
                ("CHARACTER", "SCENE", "PROP", "STYLE", "IMAGE", "SHOT_IMAGE")
            ),
        )
        .order_by(AssetVersion.created_at, AssetVersion.id)
        .limit(limit)
        .offset(offset)
    )
    return [
        ReferenceImageRead(
            id=v.id,
            created_at=v.created_at,
            updated_at=v.updated_at,
            asset_id=a.id,
            asset_name=a.name,
            asset_type=a.asset_type,
            version_no=v.version_no,
            status=v.status,
            stale=v.stale,
        )
        for v, a in rows
    ]


@router.put("/projects/{project_id}/assets/{asset_id}/character-profile")
def update_character_profile(
    project_id: UUID, asset_id: UUID, payload: CharacterProfileUpdate, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = find_asset(db, project_id, asset_id)
    if row.asset_type != "CHARACTER":
        raise DomainError("INVALID_ASSET_TYPE", "只有角色资产可以维护角色特征", 422)
    if row.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定的资产不可编辑", 409)
    validate_character_profile(db, project_id, row, payload.profile)
    change_asset(
        db,
        row,
        payload.expected_revision,
        character_profile=payload.profile.model_dump(mode="json"),
    )
    audit(
        db, project_id, actor, "asset.character_profile_updated", row.id, {"revision": row.revision}
    )
    return AssetRead.model_validate(row)


def editable_profile_asset(db: Session, project_id: UUID, asset_id: UUID, kind: str) -> VisualAsset:
    row = find_asset(db, project_id, asset_id)
    if row.asset_type != kind:
        raise DomainError("INVALID_ASSET_TYPE", "资产类型与设定类型不匹配", 422)
    if row.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定的资产不可编辑", 409)
    return row


@router.put("/projects/{project_id}/assets/{asset_id}/scene-profile")
def update_scene_profile(
    project_id: UUID, asset_id: UUID, payload: SceneProfileUpdate, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = editable_profile_asset(db, project_id, asset_id, "SCENE")
    validate_scene_profile(db, project_id, row, payload.profile)
    change_asset(
        db, row, payload.expected_revision, scene_profile=payload.profile.model_dump(mode="json")
    )
    audit(db, project_id, actor, "asset.scene_profile_updated", row.id, {"revision": row.revision})
    return AssetRead.model_validate(row)


@router.put("/projects/{project_id}/assets/{asset_id}/prop-profile")
def update_prop_profile(
    project_id: UUID, asset_id: UUID, payload: PropProfileUpdate, db: Database, actor: Actor
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    row = editable_profile_asset(db, project_id, asset_id, "PROP")
    validate_prop_profile(db, project_id, payload.profile)
    change_asset(
        db, row, payload.expected_revision, prop_profile=payload.profile.model_dump(mode="json")
    )
    audit(db, project_id, actor, "asset.prop_profile_updated", row.id, {"revision": row.revision})
    return AssetRead.model_validate(row)


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
