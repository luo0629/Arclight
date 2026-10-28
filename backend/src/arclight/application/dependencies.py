"""Capture exact inputs and register derived edges in the output transaction."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    VisualAsset,
)


def exact_version(
    db: Session, project_id: UUID, version_id: UUID
) -> tuple[AssetVersion, VisualAsset]:
    result = db.execute(
        select(AssetVersion, VisualAsset)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(AssetVersion.id == version_id, VisualAsset.project_id == project_id)
    ).first()
    if result is None:
        raise DomainError("NOT_FOUND", "精确资产版本不存在", 404)
    return result[0], result[1]


def require_manifest(db: Session, project_id: UUID, manifest_id: UUID) -> DependencyManifest:
    row = db.get(DependencyManifest, manifest_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "输入清单不存在", 404)
    return row


def capture_manifest(
    db: Session, project_id: UUID, actor: str, payload: ManifestCreate
) -> tuple[DependencyManifest, bool]:
    fingerprint = hashlib.sha256(
        json.dumps(
            payload.model_dump(mode="json", exclude={"creation_key"}), sort_keys=True
        ).encode()
    ).hexdigest()
    previous = db.scalar(
        select(DependencyManifest).where(
            DependencyManifest.project_id == project_id,
            DependencyManifest.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一保存键不能用于不同输入清单", 409)
        return previous, False
    target = db.get(VisualAsset, payload.target_asset_id)
    if target is None or target.project_id != project_id:
        raise DomainError("NOT_FOUND", "目标资产不存在", 404)
    if target.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "目标资产只读", 409)
    inputs = []
    for item in payload.inputs:
        version, asset = exact_version(db, project_id, item.source_asset_version_id)
        if (
            version.stale
            or version.status not in {"APPROVED", "LOCKED"}
            or asset.status == "ARCHIVED"
        ):
            raise DomainError("ASSET_NOT_USABLE", "输入版本必须已批准且有效", 409)
        inputs.append(
            {
                **item.model_dump(mode="json"),
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type,
                "asset_name": asset.name,
                "version_no": version.version_no,
                "checksum": version.checksum,
            }
        )
    row = DependencyManifest(
        project_id=project_id,
        target_asset_id=target.id,
        creation_key=payload.creation_key,
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={
            "schema_version": 1,
            "target": {"asset_id": str(target.id), "asset_type": target.asset_type},
            "inputs": inputs,
            "context": payload.context,
        },
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row, True


def attach_output(
    db: Session, project_id: UUID, manifest_id: UUID, output: AssetVersion
) -> list[AssetDependency]:
    manifest = require_manifest(db, project_id, manifest_id)
    if manifest.target_asset_id != output.asset_id or output.metadata_json.get(
        "dependency_manifest_id"
    ) != str(manifest.id):
        raise DomainError("INVALID_DEPENDENCY_OUTPUT", "产物必须在创建时固定其原始输入清单", 409)
    receipt = db.scalar(
        select(DependencyOutput).where(DependencyOutput.asset_version_id == output.id)
    )
    if receipt is not None:
        if receipt.dependency_manifest_id != manifest_id:
            raise DomainError("DEPENDENCY_CONFLICT", "历史产物不能改绑输入清单", 409)
        return list(
            db.scalars(
                select(AssetDependency).where(AssetDependency.target_asset_version_id == output.id)
            )
        )
    rows = [
        AssetDependency(
            source_asset_version_id=UUID(item["source_asset_version_id"]),
            target_asset_version_id=output.id,
            dependency_type=item["dependency_type"],
            dependency_manifest_id=manifest.id,
        )
        for item in manifest.snapshot_json["inputs"]
    ]
    db.add(DependencyOutput(dependency_manifest_id=manifest.id, asset_version_id=output.id))
    db.add_all(rows)
    db.flush()
    return rows


def query_dependencies(
    db: Session,
    project_id: UUID,
    *,
    version_ids: list[UUID] | None = None,
    direction: str = "outgoing",
    source: UUID | None = None,
    target: UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[AssetDependency]:
    source_version, target_version = aliased(AssetVersion), aliased(AssetVersion)
    source_asset, target_asset = aliased(VisualAsset), aliased(VisualAsset)
    statement = (
        select(AssetDependency)
        .join(source_version, source_version.id == AssetDependency.source_asset_version_id)
        .join(source_asset, source_asset.id == source_version.asset_id)
        .join(target_version, target_version.id == AssetDependency.target_asset_version_id)
        .join(target_asset, target_asset.id == target_version.asset_id)
        .where(source_asset.project_id == project_id, target_asset.project_id == project_id)
    )
    if source is not None:
        exact_version(db, project_id, source)
        statement = statement.where(AssetDependency.source_asset_version_id == source)
    if target is not None:
        exact_version(db, project_id, target)
        statement = statement.where(AssetDependency.target_asset_version_id == target)
    if version_ids is not None:
        visible = set(
            db.scalars(
                select(AssetVersion.id)
                .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
                .where(AssetVersion.id.in_(version_ids), VisualAsset.project_id == project_id)
            )
        )
        if visible != set(version_ids):
            raise DomainError("NOT_FOUND", "精确资产版本不存在", 404)
        column = (
            AssetDependency.source_asset_version_id
            if direction == "outgoing"
            else AssetDependency.target_asset_version_id
        )
        statement = statement.where(column.in_(version_ids))
    return list(
        db.scalars(
            statement.order_by(AssetDependency.created_at, AssetDependency.id)
            .limit(limit)
            .offset(offset)
        )
    )
