"""Capture exact inputs and register derived edges in the output transaction."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate, validate_dependency_type
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    DependencyInvalidation,
    DependencyManifest,
    DependencyOutput,
    Project,
    VisualAsset,
)


def lock_dependency_graph(db: Session, project_id: UUID) -> None:
    """Serialize validation and writes, including callers outside HTTP routes."""
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        assert driver is not None
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    project = db.get(Project, project_id, populate_existing=True, with_for_update=True)
    if project is None or project.deleted_at is not None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
    if project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目只允许查看", 409)


def reject_dependency_cycle(
    db: Session, project_id: UUID, target_id: UUID, source_ids: list[UUID]
) -> None:
    # UNION (not UNION ALL) terminates even if imported legacy data has a cycle.
    reachable = (
        select(AssetVersion.id.label("version_id"))
        .where(AssetVersion.id == target_id)
        .cte("dependency_reachable", recursive=True)
    )
    reachable = reachable.union(
        select(AssetDependency.target_asset_version_id)
        .join(reachable, AssetDependency.source_asset_version_id == reachable.c.version_id)
        .join(AssetVersion, AssetVersion.id == AssetDependency.target_asset_version_id)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(VisualAsset.project_id == project_id)
    )
    if (
        db.scalar(
            select(reachable.c.version_id).where(reachable.c.version_id.in_(source_ids)).limit(1)
        )
        is not None
    ):
        raise DomainError("DEPENDENCY_CYCLE", "依赖不能形成自环或循环", 409)


def exact_version(
    db: Session, project_id: UUID, version_id: UUID
) -> tuple[AssetVersion, VisualAsset]:
    result = db.execute(
        select(AssetVersion, VisualAsset)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(AssetVersion.id == version_id, VisualAsset.project_id == project_id)
        .execution_options(populate_existing=True)
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
    lock_dependency_graph(db, project_id)
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
    target = db.get(VisualAsset, payload.target_asset_id, populate_existing=True)
    if target is None or target.project_id != project_id:
        raise DomainError("NOT_FOUND", "目标资产不存在", 404)
    if target.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "目标资产只读", 409)
    inputs = []
    for item in payload.inputs:
        version, asset = exact_version(db, project_id, item.source_asset_version_id)
        validate_dependency_type(asset.asset_type, target.asset_type, item.dependency_type)
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
            "invalidation_seq": db.scalar(
                select(func.max(DependencyInvalidation.event_no)).where(
                    DependencyInvalidation.project_id == project_id
                )
            )
            or 0,
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
    lock_dependency_graph(db, project_id)
    manifest = require_manifest(db, project_id, manifest_id)
    output, target = exact_version(db, project_id, output.id)
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
    source_ids = []
    for item in manifest.snapshot_json["inputs"]:
        version, source = exact_version(db, project_id, UUID(item["source_asset_version_id"]))
        if (
            item["asset_id"] != str(source.id)
            or item["asset_type"] != source.asset_type
            or item["version_no"] != version.version_no
            or item["checksum"] != version.checksum
        ):
            raise DomainError("INVALID_DEPENDENCY_INPUT", "输入清单与精确版本身份不匹配", 409)
        validate_dependency_type(source.asset_type, target.asset_type, item["dependency_type"])
        source_ids.append(version.id)
    reject_dependency_cycle(db, project_id, output.id, source_ids)
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
    from arclight.application.invalidation import input_expired, mark_stale

    if any(
        input_expired(
            db,
            project_id,
            item,
            target.asset_type,
            after_event_no=manifest.snapshot_json.get("invalidation_seq", 0),
        )
        for item in manifest.snapshot_json["inputs"]
    ):
        mark_stale(db, output)
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
