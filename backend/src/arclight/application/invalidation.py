"""Project-serialized, exact-version stale propagation without rewriting provenance."""

import hashlib
import json
from collections import deque
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.dependencies import (
    exact_version,
    input_usable,
    invalidation_sequence,
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
    VisualAsset,
)

if TYPE_CHECKING:
    from arclight.application.narrative_dependencies import NarrativeReplacement


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


def _item(version: AssetVersion, asset: VisualAsset, depth: int, source: dict) -> dict:
    return {
        "asset_version_id": str(version.id),
        "asset_id": str(asset.id),
        "asset_type": asset.asset_type,
        "asset_name": asset.name,
        "version_no": version.version_no,
        "revision": version.revision,
        "status": version.status,
        "stale": version.stale,
        "depth": depth,
        "action": required_action(asset.asset_type),
        **source,
    }


def impacts(
    db: Session,
    project_id: UUID,
    changes: list[dict],
    *,
    narrative: "NarrativeReplacement | None" = None,
) -> list[dict]:
    """Compute the complete closure, or fail explicitly rather than truncate it."""
    from arclight.application.impact_details import describe_impacts

    roots = []
    if narrative is not None:
        from arclight.application.narrative_dependencies import narrative_consumers

        roots = narrative_consumers(db, project_id, narrative)
    if not changes and not roots:
        return []
    edges = query_dependencies(db, project_id, limit=10001)
    if len(edges) > 10000:
        raise DomainError("DEPENDENCY_GRAPH_TOO_LARGE", "依赖图超出本次分析上限", 409)
    outgoing: dict[UUID, list] = {}
    for edge in edges:
        outgoing.setdefault(edge.source_asset_version_id, []).append(edge)
    queue = deque((change, 0, None) for change in changes)
    found: dict[str, dict] = {}

    def enqueue(item: dict) -> None:
        key = item["asset_version_id"]
        found[key] = item
        queue.append(
            (
                {"source_asset_version_id": key, "change_kind": "LIFECYCLE", "selectors": []},
                item["depth"],
                item.get("reason"),
            )
        )

    for version, asset, source in roots:
        enqueue(_item(version, asset, 1, source))
    while queue:
        change, depth, upstream_reason = queue.popleft()
        source_id = UUID(change["source_asset_version_id"])
        for edge in outgoing.get(source_id, []):
            version, asset = exact_version(db, project_id, edge.target_asset_version_id)
            if str(version.id) in found or not affects_input(
                change, edge_input(db, edge), asset.asset_type
            ):
                continue
            enqueue(
                _item(
                    version,
                    asset,
                    depth + 1,
                    {
                        "source_asset_version_id": str(source_id),
                        "dependency_type": edge.dependency_type,
                        "reason": upstream_reason or "依赖的精确来源发生变更",
                    },
                )
            )
    ordered = sorted(found.values(), key=lambda item: (item["depth"], item["asset_version_id"]))
    describe_impacts(db, project_id, ordered)
    return ordered


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


def impact_preview(
    db: Session,
    project_id: UUID,
    changes: list[dict],
    *,
    narrative: "NarrativeReplacement | None" = None,
) -> dict:
    validate_changes(db, project_id, changes)
    affected = impacts(db, project_id, changes, narrative=narrative)
    scope = [narrative.snapshot(), affected] if narrative is not None else affected
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return {
        "affected": affected,
        "digest": digest,
        "summary": {
            action: sum(x["action"] == action for x in affected)
            for action in ("REVIEW", "GENERATE", "RENDER")
        },
    }


def check_impact(
    db: Session,
    project_id: UUID,
    changes: list[dict],
    expected: str | None,
    *,
    narrative: "NarrativeReplacement | None" = None,
) -> None:
    if expected is None and narrative is None:
        return
    lock_dependency_graph(db, project_id)
    preview = impact_preview(db, project_id, changes, narrative=narrative)
    if expected is None:
        if any("source_kind" in item for item in preview["affected"]):
            raise DomainError("DEPENDENCY_IMPACT_REQUIRED", "请预览并确认跨集影响", 409)
    elif preview["digest"] != expected:
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
    db: Session,
    project_id: UUID,
    item: dict,
    target_kind: str,
    *,
    after_event_no: int = 0,
    authorized_reference: UUID | None = None,
) -> bool:
    version, asset = exact_version(db, project_id, UUID(item["source_asset_version_id"]))
    production_frame = (
        version.id == authorized_reference
        and item["dependency_type"] == "REFERENCE_DEPENDENCY"
        and target_kind == "VIDEO"
    )
    if not production_frame and not input_usable(db, version, asset, target_kind):
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
    *,
    narrative: "NarrativeReplacement | None" = None,
) -> tuple[DependencyInvalidation, bool]:
    lock_dependency_graph(db, project_id)
    request: dict[str, Any] = {"changes": changes, "reason": reason}
    if narrative is not None:
        request["narrative"] = narrative.snapshot()
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
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
    affected = impacts(db, project_id, changes, narrative=narrative)
    # Late descendants also need a concrete lifecycle event for each screenplay.
    roots = [
        {
            "source_asset_version_id": item["asset_version_id"],
            "expected_revision": item["revision"],
            "change_kind": "LIFECYCLE",
            "selectors": [],
        }
        for item in affected
        if "source_kind" in item
    ]
    for item in affected:
        version, _ = exact_version(db, project_id, UUID(item["asset_version_id"]))
        item["changed"] = mark_stale(db, version)
        item.update(
            status_after=version.status, stale_after=version.stale, revision_after=version.revision
        )
    event = DependencyInvalidation(
        project_id=project_id,
        creation_key=creation_key,
        event_no=invalidation_sequence(db, project_id) + 1,
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={**request, "changes": [*changes, *roots], "affected": affected},
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    audit(
        db,
        project_id,
        actor,
        "dependency.invalidated",
        event.id,
        {
            "creation_key": str(event.creation_key),
            "event_no": event.event_no,
            **event.snapshot_json,
        },
    )
    return event, True
