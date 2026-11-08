"""Append-only manual image versions with separate, CAS-protected lifecycle state."""

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import func, select, update

from arclight.api.assets import AssetRead, change_asset, find_asset
from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.rights import UsageDeclaration
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.asset_objects import object_key, read_object, write_object
from arclight.application.asset_provenance import capture_provenance
from arclight.application.commands import require_project
from arclight.application.dependencies import attach_output, require_manifest
from arclight.application.dependency_rebuild import (
    RebuildInputs,
    original_manifest,
    rebuild_manifest,
)
from arclight.application.files import record_asset_file
from arclight.application.generation_candidates import require_candidate_review
from arclight.application.invalidation import check_impact, invalidate
from arclight.core.errors import DomainError
from arclight.domain.asset_provenance import GenerationDeclaration
from arclight.domain.asset_versions import MAX_IMAGE_BASE64, decode_image, next_status
from arclight.domain.file_policy import check_file_policy
from arclight.persistence.models import AssetSelectionLock, AssetVersion, VisualAsset

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/assets/{asset_id}/versions", tags=["asset-versions"]
)


class VersionCreate(UsageDeclaration):
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    original_name: str = Field(min_length=1, max_length=160)
    media_type: Literal["image/png", "image/jpeg"]
    content_base64: str = Field(min_length=1, max_length=MAX_IMAGE_BASE64)
    generation: GenerationDeclaration | None = None
    dependency_manifest_id: UUID | None = None

    @field_validator("original_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value) or "/" in value or "\\" in value:
            raise ValueError("文件名不能包含路径或控制字符")
        return value


class VersionState(Input):
    expected_revision: int = Field(strict=True, ge=0)
    status: Literal["READY_FOR_REVIEW", "APPROVED", "REJECTED", "STALE", "ARCHIVED", "RESTORE"]
    comment: str = Field(default="", max_length=1000)
    impact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class VersionRead(Resource):
    asset_id: UUID
    version_no: int
    checksum: str
    created_by: str
    status: str
    stale: bool
    revision: int
    archived_from_status: str | None
    # Only this service's allowlisted, server-produced metadata is public.
    media: dict | None
    asset_snapshot: dict | None
    restored_from_version_id: UUID | None = None
    restoration_note: str | None = None
    provenance: dict | None = None
    dependency_manifest_id: UUID | None = None
    required_review: str | None = None


def read_version(row: AssetVersion) -> VersionRead:
    local = row.creation_key is not None and row.metadata_json.get("storage_backend") in {
        "LOCAL",
        "S3",
    }
    return VersionRead(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        asset_id=row.asset_id,
        version_no=row.version_no,
        checksum=row.checksum,
        created_by=row.created_by,
        status=row.status,
        stale=row.stale,
        revision=row.revision,
        archived_from_status=row.archived_from_status,
        media=row.metadata_json.get("media") if local else None,
        asset_snapshot=row.metadata_json.get("asset_snapshot") if local else None,
        restored_from_version_id=row.metadata_json.get("restored_from_version_id"),
        restoration_note=row.metadata_json.get("restoration_note"),
        provenance=row.metadata_json.get("provenance") if local else None,
        dependency_manifest_id=row.metadata_json.get("dependency_manifest_id"),
        required_review=row.metadata_json.get("required_review"),
    )


def find_version(db: Database, asset_id: UUID, version_id: UUID) -> AssetVersion:
    row = db.get(AssetVersion, version_id, populate_existing=True)
    if row is None or row.asset_id != asset_id:
        raise DomainError("NOT_FOUND", "资产版本不存在", 404)
    return row


@router.get("")
def list_versions(
    project_id: UUID,
    asset_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[VersionRead]:
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id, include_media=True)
    return [
        read_version(row)
        for row in db.scalars(
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id)
            .order_by(AssetVersion.version_no, AssetVersion.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/{version_id}")
def get_version(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    db: Database,
    actor: Actor,
) -> VersionRead:
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id, include_media=True)
    return read_version(find_version(db, asset_id, version_id))


@router.get("/{version_id}/image")
def get_image(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    request: Request,
    db: Database,
    actor: Actor,
) -> Response:
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id, include_media=True)
    row = find_version(db, asset_id, version_id)
    if row.creation_key is None or row.metadata_json.get("storage_backend") not in {"LOCAL", "S3"}:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "此历史图片尚未接入本机读取", 409)
    data = read_object(
        request.app.state.settings,
        project_id,
        row.id,
        row.object_key,
        row.checksum,
    )
    return Response(
        data,
        media_type=row.metadata_json["media"]["media_type"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="asset-image"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.post("", status_code=201)
def create_version(
    project_id: UUID,
    asset_id: UUID,
    payload: VersionCreate,
    request: Request,
    db: Database,
    actor: Actor,
) -> VersionRead:
    require_project(db, project_id, actor, write=True)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    request_values = payload.model_dump(mode="json", exclude={"expected_revision", "creation_key"})
    # Preserve idempotency fingerprints issued before optional DAG manifests existed.
    if payload.dependency_manifest_id is None:
        request_values.pop("dependency_manifest_id")
    fingerprint = hashlib.sha256(
        json.dumps(
            request_values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    existing = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset_id, AssetVersion.creation_key == payload.creation_key
        )
    )
    if existing is not None:
        if existing.metadata_json.get("request_sha256") != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一导入标识不能用于不同图片或资料", 409)
        return read_version(existing)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定资产不能创建新版本", 409)
    if payload.dependency_manifest_id is not None:
        manifest = require_manifest(db, project_id, payload.dependency_manifest_id)
        if manifest.target_asset_id != asset.id:
            raise DomainError("INVALID_DEPENDENCY_OUTPUT", "输入清单的目标资产不匹配", 409)
    check_file_policy("REFERENCE_IMAGE", payload.original_name, payload.media_type, 1)
    image = decode_image(payload.content_base64, payload.media_type)
    provenance = capture_provenance(db, project_id, image, payload.generation)
    snapshot = AssetRead.model_validate(asset).model_dump(mode="json")
    change_asset(db, asset, payload.expected_revision)
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset_id)
        )
        or 0
    ) + 1
    row = AssetVersion(
        id=uuid4(),
        asset_id=asset_id,
        version_no=number,
        checksum=image.checksum,
        created_by=actor,
        creation_key=payload.creation_key,
        status="DRAFT",
        metadata_json={
            "storage_backend": request.app.state.settings.object_storage_backend.upper(),
            **(
                {"required_review": "CONTINUITY"}
                if asset.asset_type in {"IMAGE", "SHOT_IMAGE"}
                else {}
            ),
            "dependency_manifest_id": (
                str(payload.dependency_manifest_id) if payload.dependency_manifest_id else None
            ),
            "provenance": provenance,
            "request_sha256": fingerprint,
            "asset_snapshot": snapshot,
            "media": {
                "media_type": image.media_type,
                "byte_size": len(image.data),
                "width": image.width,
                "height": image.height,
                "original_name": payload.original_name,
                "source_note": payload.source_note,
                "rights_note": payload.rights_note,
                "rights_confirmed": True,
            },
        },
    )
    row.object_key = object_key(project_id, row.id, request.app.state.settings)
    write_object(db, request.app.state.settings, project_id, row.id, image.data, image.media_type)
    db.add(row)
    db.flush()
    db.refresh(row)
    record_asset_file(db, request.app.state.settings, project_id, row)
    if payload.dependency_manifest_id is not None:
        attach_output(db, project_id, payload.dependency_manifest_id, row)
    audit(
        db,
        project_id,
        actor,
        "asset.version_created",
        row.id,
        {"asset_id": str(asset_id), "version_no": number, "checksum": row.checksum},
    )
    return read_version(row)


class VersionSelection(Input):
    expected_revision: int = Field(strict=True, ge=0)
    expected_lock_version: int = Field(strict=True, ge=0)
    impact_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class VersionRestore(Input):
    expected_revision: int = Field(strict=True, ge=0)
    creation_key: UUID
    comment: str = Field(min_length=1, max_length=1000)
    dependency_manifest_id: UUID | None = None


@router.get("/{version_id}/rebuild-inputs")
def get_rebuild_inputs(
    project_id: UUID, asset_id: UUID, version_id: UUID, db: Database, actor: Actor
) -> dict:
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id, include_media=True)
    version = find_version(db, asset_id, version_id)
    manifest = original_manifest(db, project_id, version)
    inputs = []
    from arclight.application.dependencies import exact_version

    for item in manifest.snapshot_json["inputs"]:
        source, asset = exact_version(db, project_id, UUID(item["source_asset_version_id"]))
        inputs.append(
            {
                **item,
                "status": source.status,
                "stale": source.stale,
                "current_version_id": str(asset.current_version_id)
                if asset.current_version_id
                else None,
            }
        )
    return {"inputs": inputs, "original_manifest_id": str(manifest.id)}


@router.post("/{version_id}/rebuild-manifest", status_code=201)
def create_rebuild_manifest(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: RebuildInputs,
    db: Database,
    actor: Actor,
) -> dict:
    require_project(db, project_id, actor, write=True)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "归档或锁定资产不能创建重建清单", 409)
    row, _ = rebuild_manifest(
        db, project_id, find_version(db, asset_id, version_id), actor, payload
    )
    return {"id": str(row.id), "snapshot": row.snapshot_json}


@router.post("/{version_id}/select")
def select_version(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionSelection,
    request: Request,
    db: Database,
    actor: Actor,
) -> AssetRead:
    require_project(db, project_id, actor, write=True)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    row = find_version(db, asset_id, version_id)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定资产不能更改选用", 409)
    require_candidate_review(row, db)
    if row.status != "APPROVED" or row.stale:
        raise DomainError("ASSET_NOT_USABLE", "只能选用已批准且有效的精确版本", 409)
    read_object(
        request.app.state.settings,
        project_id,
        row.id,
        row.object_key,
        row.checksum,
    )
    previous = asset.current_version_id
    if previous is not None and previous != row.id:
        old = find_version(db, asset_id, previous)
        check_impact(
            db,
            project_id,
            [
                {
                    "source_asset_version_id": str(old.id),
                    "expected_revision": old.revision,
                    "change_kind": "CONTENT",
                    "selectors": [],
                }
            ],
            payload.impact_digest,
        )
    change_selection(db, asset, payload, current_version_id=row.id)
    propagation_id = None
    if previous is not None and previous != row.id:
        old = find_version(db, asset_id, previous)
        propagation, _ = invalidate(
            db,
            project_id,
            actor,
            uuid5(asset.id, f"selection:{asset.lock_version}"),
            [
                {
                    "source_asset_version_id": str(old.id),
                    "expected_revision": old.revision,
                    "change_kind": "CONTENT",
                    "selectors": [],
                }
            ],
            "正式选用已切换到新版本",
        )
        propagation_id = str(propagation.id)
    audit(
        db,
        project_id,
        actor,
        "asset.version_selected",
        asset.id,
        {
            "previous_version_id": str(previous) if previous else None,
            "version_id": str(row.id),
            "revision": asset.revision,
            "lock_version": asset.lock_version,
            "dependency_invalidation_id": propagation_id,
        },
    )
    return AssetRead.model_validate(asset)


def change_selection(db: Database, asset: VisualAsset, payload: VersionSelection, **values) -> None:
    changed = db.scalar(
        update(VisualAsset)
        .where(
            VisualAsset.id == asset.id,
            VisualAsset.revision == payload.expected_revision,
            VisualAsset.lock_version == payload.expected_lock_version,
        )
        .values(
            **values, revision=VisualAsset.revision + 1, lock_version=VisualAsset.lock_version + 1
        )
        .returning(VisualAsset.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "资产选用或锁定状态已改变，请刷新后重试", 409)
    db.refresh(asset)


def change_lock(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionSelection,
    request: Request,
    db: Database,
    actor: Actor,
    *,
    locked: bool,
) -> AssetRead:
    require_project(db, project_id, actor, write=True, owner=True)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    version = find_version(db, asset_id, version_id)
    record = db.scalar(select(AssetSelectionLock).where(AssetSelectionLock.asset_id == asset_id))
    if asset.current_version_id != version.id or asset.status == "ARCHIVED":
        raise DomainError("ASSET_SELECTION_CONFLICT", "只能锁定或解锁当前选用的精确版本", 409)
    before = asset.status
    if locked:
        require_candidate_review(version, db)
        if (
            before == "LOCKED"
            or record is not None
            or version.status != "APPROVED"
            or version.stale
        ):
            raise DomainError("ASSET_LOCK_CONFLICT", "选用版本未批准、失效或已锁定", 409)
        read_object(
            request.app.state.settings,
            project_id,
            version.id,
            version.object_key,
            version.checksum,
        )
        change_selection(db, asset, payload, status="LOCKED")
        db.add(
            AssetSelectionLock(
                asset_id=asset.id, version_id=version.id, previous_status=before, created_by=actor
            )
        )
        version.status = "LOCKED"
    else:
        if (
            before != "LOCKED"
            or record is None
            or record.version_id != version.id
            or version.status != "LOCKED"
        ):
            raise DomainError("ASSET_LOCK_CONFLICT", "没有可安全解除的精确选用锁", 409)
        change_selection(db, asset, payload, status=record.previous_status)
        db.delete(record)
        version.status = "APPROVED"
    version.revision += 1
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "asset.selection_locked" if locked else "asset.selection_unlocked",
        asset.id,
        {
            "version_id": str(version.id),
            "from": before,
            "to": asset.status,
            "lock_version": asset.lock_version,
            "revision": asset.revision,
        },
    )
    return AssetRead.model_validate(asset)


@router.post("/{version_id}/lock")
def lock_selection(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionSelection,
    request: Request,
    db: Database,
    actor: Actor,
) -> AssetRead:
    return change_lock(project_id, asset_id, version_id, payload, request, db, actor, locked=True)


@router.post("/{version_id}/unlock")
def unlock_selection(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionSelection,
    request: Request,
    db: Database,
    actor: Actor,
) -> AssetRead:
    return change_lock(project_id, asset_id, version_id, payload, request, db, actor, locked=False)


@router.post("/{version_id}/restore-as-new", status_code=201)
def restore_version_as_new(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionRestore,
    request: Request,
    db: Database,
    actor: Actor,
) -> VersionRead:
    require_project(db, project_id, actor, write=True)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    source = find_version(db, asset_id, version_id)
    existing = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset_id,
            AssetVersion.creation_key == payload.creation_key,
        )
    )
    if existing is not None:
        if (
            existing.metadata_json.get("restored_from_version_id") != str(source.id)
            or existing.metadata_json.get("restoration_note") != payload.comment
            or existing.metadata_json.get("dependency_manifest_id")
            != (str(payload.dependency_manifest_id) if payload.dependency_manifest_id else None)
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一恢复标识不能用于不同版本或说明", 409)
        return read_version(existing)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定资产不能恢复为新版本", 409)
    if source.creation_key is None or source.metadata_json.get("storage_backend") not in {
        "LOCAL",
        "S3",
    }:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "此历史图片尚未接入本机读取", 409)
    if (
        source.metadata_json.get("dependency_manifest_id")
        and payload.dependency_manifest_id is None
    ):
        raise DomainError("DEPENDENCY_MANIFEST_REQUIRED", "派生图片须先确认新输入清单后另存", 409)
    if payload.dependency_manifest_id is not None:
        manifest = require_manifest(db, project_id, payload.dependency_manifest_id)
        if manifest.target_asset_id != asset.id or manifest.snapshot_json["context"].get(
            "rebuild_from_version_id"
        ) != str(source.id):
            raise DomainError("INVALID_REBUILD_INPUT", "重建清单与原版本不匹配", 409)
    data = read_object(
        request.app.state.settings,
        project_id,
        source.id,
        source.object_key,
        source.checksum,
    )
    snapshot = AssetRead.model_validate(asset).model_dump(mode="json")
    change_asset(db, asset, payload.expected_revision)
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset_id)
        )
        or 0
    ) + 1
    row = AssetVersion(
        id=uuid4(),
        asset_id=asset_id,
        version_no=number,
        checksum=source.checksum,
        created_by=actor,
        creation_key=payload.creation_key,
        status="DRAFT",
        stale=False,
        metadata_json={
            "storage_backend": request.app.state.settings.object_storage_backend.upper(),
            "asset_snapshot": snapshot,
            "media": dict(source.metadata_json["media"]),
            "restored_from_version_id": str(source.id),
            "restoration_note": payload.comment,
            "required_review": (
                "CONTINUITY"
                if asset.asset_type == "SHOT_IMAGE"
                else source.metadata_json.get("required_review")
            ),
            "provenance": source.metadata_json.get("provenance"),
            "dependency_manifest_id": str(payload.dependency_manifest_id)
            if payload.dependency_manifest_id
            else None,
        },
    )
    row.object_key = object_key(project_id, row.id, request.app.state.settings)
    write_object(
        db,
        request.app.state.settings,
        project_id,
        row.id,
        data,
        source.metadata_json["media"]["media_type"],
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    record_asset_file(db, request.app.state.settings, project_id, row)
    if payload.dependency_manifest_id is not None:
        attach_output(db, project_id, payload.dependency_manifest_id, row)
    audit(
        db,
        project_id,
        actor,
        "asset.version_restored_as_new",
        row.id,
        {
            "asset_id": str(asset_id),
            "source_version_id": str(source.id),
            "version_no": number,
            "comment": payload.comment,
        },
    )
    return read_version(row)


@router.patch("/{version_id}/state")
def update_version_state(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    payload: VersionState,
    request: Request,
    db: Database,
    actor: Actor,
) -> VersionRead:
    require_project(
        db, project_id, actor, write=True, owner=payload.status in {"APPROVED", "REJECTED", "STALE"}
    )
    asset = find_asset(db, project_id, asset_id, include_media=True)
    row = find_version(db, asset_id, version_id)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定资产只允许查看版本", 409)
    if row.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "资产版本状态已改变，请刷新后重试", 409)
    target = next_status(row.status, payload.status, row.archived_from_status)
    if asset.current_version_id == row.id and target == "ARCHIVED":
        raise DomainError("ASSET_VERSION_IN_USE", "当前选用版本不能直接归档", 409)
    if payload.status in {"REJECTED", "STALE"} and not payload.comment:
        raise DomainError("REVIEW_COMMENT_REQUIRED", "拒绝或标记失效必须说明原因", 422)
    if target in {"READY_FOR_REVIEW", "APPROVED"}:
        require_candidate_review(row, db)
        if row.creation_key is None or row.metadata_json.get("storage_backend") not in {
            "LOCAL",
            "S3",
        }:
            raise DomainError("ASSET_OBJECT_UNAVAILABLE", "历史对象须接入并核验后才能重新审核", 409)
        read_object(
            request.app.state.settings,
            project_id,
            row.id,
            row.object_key,
            row.checksum,
        )
    before = row.status
    if target in {"STALE", "ARCHIVED", "REJECTED"}:
        check_impact(
            db,
            project_id,
            [
                {
                    "source_asset_version_id": str(row.id),
                    "expected_revision": row.revision,
                    "change_kind": "LIFECYCLE",
                    "selectors": [],
                }
            ],
            payload.impact_digest,
        )
    changed = db.scalar(
        update(AssetVersion)
        .where(AssetVersion.id == row.id, AssetVersion.revision == payload.expected_revision)
        .values(
            status=target,
            stale=row.stale or target == "STALE",
            revision=AssetVersion.revision + 1,
            archived_from_status=before if target == "ARCHIVED" else None,
        )
        .returning(AssetVersion.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "资产版本状态已改变，请刷新后重试", 409)
    db.refresh(row)
    if target in {"STALE", "ARCHIVED", "REJECTED"}:
        invalidate(
            db,
            project_id,
            actor,
            uuid5(row.id, f"state:{row.revision}"),
            [
                {
                    "source_asset_version_id": str(row.id),
                    "expected_revision": row.revision,
                    "change_kind": "LIFECYCLE",
                    "selectors": [],
                }
            ],
            payload.comment or "来源版本生命周期已变化",
        )
    audit(
        db,
        project_id,
        actor,
        "asset.version_state_changed",
        row.id,
        {
            "asset_id": str(asset_id),
            "from": before,
            "to": target,
            "revision": row.revision,
            "comment": payload.comment,
        },
    )
    return read_version(row)
