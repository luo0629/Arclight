"""Project-serialized, exact-version stale propagation without rewriting provenance."""

import hashlib
import json
from collections import deque
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.dependencies import (
    exact_version,
    lock_dependency_graph,
    query_dependencies,
)
from arclight.core.errors import DomainError
from arclight.domain.invalidation import affects_input, required_action
from arclight.persistence.models import (
    AssetVersion,
    DependencyInvalidation,
    DependencyManifest,
    Script,
    Shot,
    Storyboard,
)


def edge_input(db: Session, edge) -> dict:
    if edge.dependency_manifest_id:
        manifest = db.get(DependencyManifest, edge.dependency_manifest_id)
        if manifest:
            for item in manifest.snapshot_json["inputs"]:
                if (
                    item["source_asset_version_id"] == str(edge.source_asset_version_id)
                    and item["dependency_type"] == edge.dependency_type
                ):
                    return item
    return {
        "source_asset_version_id": str(edge.source_asset_version_id),
        "dependency_type": edge.dependency_type,
        "selectors": [],
    }


def impacts(db: Session, project_id: UUID, changes: list[dict]) -> list[dict]:
    """Compute the complete closure, or fail explicitly rather than truncate it."""
    edges = query_dependencies(db, project_id, limit=10001)
    if len(edges) > 10000:
        raise DomainError("DEPENDENCY_GRAPH_TOO_LARGE", "依赖图超出本次分析上限", 409)
    outgoing: dict[UUID, list] = {}
    for edge in edges:
        outgoing.setdefault(edge.source_asset_version_id, []).append(edge)
    queue = deque((change, 0) for change in changes)
    found: dict[str, dict] = {}
    while queue:
        change, depth = queue.popleft()
        source_id = UUID(change["source_asset_version_id"])
        for edge in outgoing.get(source_id, []):
            version, asset = exact_version(db, project_id, edge.target_asset_version_id)
            if not affects_input(change, edge_input(db, edge), asset.asset_type):
                continue
            key = str(version.id)
            if key in found:
                continue
            found[key] = {
                "asset_version_id": key,
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type,
                "asset_name": asset.name,
                "version_no": version.version_no,
                "revision": version.revision,
                "status": version.status,
                "stale": version.stale,
                "source_asset_version_id": str(source_id),
                "dependency_type": edge.dependency_type,
                "depth": depth + 1,
                "action": required_action(asset.asset_type),
            }
            queue.append(
                (
                    {"source_asset_version_id": key, "change_kind": "LIFECYCLE", "selectors": []},
                    depth + 1,
                )
            )
    return sorted(found.values(), key=lambda item: (item["depth"], item["asset_version_id"]))


def validate_changes(db: Session, project_id: UUID, changes: list[dict]) -> None:
    for change in changes:
        source, asset = exact_version(db, project_id, UUID(change["source_asset_version_id"]))
        if source.revision != change["expected_revision"]:
            raise DomainError("VERSION_CONFLICT", "来源版本状态已改变，请刷新后重试", 409)
        if change["change_kind"] == "DIALOGUE" and asset.asset_type != "SCRIPT":
            raise DomainError("INVALID_CHANGE_SCOPE", "对白变更只能指定剧本版本", 422)
        if change["change_kind"] == "SEQUENCE" and asset.asset_type not in {
            "STORYBOARD",
            "TIMELINE",
        }:
            raise DomainError("INVALID_CHANGE_SCOPE", "顺序变更只能指定分镜或时间轴", 422)


def impact_preview(db: Session, project_id: UUID, changes: list[dict]) -> dict:
    validate_changes(db, project_id, changes)
    affected = impacts(db, project_id, changes)
    digest = hashlib.sha256(json.dumps(affected, sort_keys=True).encode()).hexdigest()
    return {
        "affected": affected,
        "digest": digest,
        "summary": {
            action: sum(x["action"] == action for x in affected)
            for action in ("REVIEW", "GENERATE", "RENDER")
        },
    }


def check_impact(db: Session, project_id: UUID, changes: list[dict], expected: str | None) -> None:
    if expected is not None:
        lock_dependency_graph(db, project_id)
        if impact_preview(db, project_id, changes)["digest"] != expected:
            raise DomainError("DEPENDENCY_IMPACT_CHANGED", "下游影响已变化，请重新预览后确认", 409)


def mark_stale(db: Session, version: AssetVersion) -> bool:
    if version.stale:
        return False
    version.stale = True
    # A selected lock remains unlockable; archive/rejection history remains visible.
    if version.status not in {"LOCKED", "ARCHIVED", "REJECTED"}:
        version.status = "STALE"
    version.revision += 1
    for model in (Script, Storyboard):
        content = cast(Script | Storyboard | None, db.get(model, version.id))
        if content is not None and content.status in {"APPROVED", "READY_FOR_REVIEW"}:
            content.status = "STALE"
            if isinstance(content, Storyboard):
                for shot in db.scalars(select(Shot).where(Shot.storyboard_id == content.id)):
                    shot.status = "STALE"
    return True


def input_expired(
    db: Session, project_id: UUID, item: dict, target_kind: str, *, after_event_no: int = 0
) -> bool:
    version, asset = exact_version(db, project_id, UUID(item["source_asset_version_id"]))
    if version.stale or version.status not in {"APPROVED", "LOCKED"} or asset.status == "ARCHIVED":
        return True
    for event in db.scalars(
        select(DependencyInvalidation).where(
            DependencyInvalidation.project_id == project_id,
            DependencyInvalidation.event_no > after_event_no,
        )
    ):
        if any(
            affects_input(change, item, target_kind) for change in event.snapshot_json["changes"]
        ):
            return True
    return False


def invalidate(
    db: Session,
    project_id: UUID,
    actor: str,
    creation_key: UUID,
    changes: list[dict],
    reason: str,
) -> tuple[DependencyInvalidation, bool]:
    lock_dependency_graph(db, project_id)
    fingerprint = hashlib.sha256(
        json.dumps({"changes": changes, "reason": reason}, sort_keys=True).encode()
    ).hexdigest()
    previous = db.scalar(
        select(DependencyInvalidation).where(
            DependencyInvalidation.project_id == project_id,
            DependencyInvalidation.creation_key == creation_key,
        )
    )
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一事件标识不能用于不同变更", 409)
        return previous, False
    validate_changes(db, project_id, changes)
    affected = impacts(db, project_id, changes)
    for item in affected:
        version, _ = exact_version(db, project_id, UUID(item["asset_version_id"]))
        item["changed"] = mark_stale(db, version)
    event = DependencyInvalidation(
        project_id=project_id,
        creation_key=creation_key,
        event_no=(
            db.scalar(
                select(func.max(DependencyInvalidation.event_no)).where(
                    DependencyInvalidation.project_id == project_id
                )
            )
            or 0
        )
        + 1,
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={"changes": changes, "reason": reason, "affected": affected},
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event, True
