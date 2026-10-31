"""Register actual text identities in the exact-version graph; no copied text objects."""

import hashlib
import json
from typing import cast
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_content
from arclight.application.audit import audit
from arclight.application.dependencies import attach_output, capture_manifest, lock_dependency_graph
from arclight.application.invalidation import check_impact, invalidate
from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate
from arclight.persistence.models import AssetVersion, Episode, Script, Storyboard, VisualAsset


def content_asset(db: Session, episode: Episode, kind: str) -> VisualAsset:
    identity = uuid5(episode.id, "dependency:" + kind)
    asset = db.get(VisualAsset, identity)
    if asset is None:
        asset = VisualAsset(
            id=identity,
            project_id=episode.project_id,
            asset_type=kind,
            name=f"第{episode.episode_no}集 · {'剧本' if kind == 'SCRIPT' else '分镜'}",
            metadata_json={"episode_id": str(episode.id), "content_registry": True},
        )
        db.add(asset)
        db.flush()
    return asset


def reference_ids(value) -> set[str]:
    if isinstance(value, list):
        return set().union(*(reference_ids(item) for item in value))
    if isinstance(value, dict):
        if "asset_id" in value and "asset_version_id" in value:
            return {value["asset_version_id"]}
        return set().union(*(reference_ids(item) for item in value.values()))
    return set()


def register_content(db: Session, row: Script | Storyboard, actor: str) -> AssetVersion:
    episode = db.get(Episode, row.episode_id)
    assert episode is not None
    lock_dependency_graph(db, episode.project_id)
    existing = db.get(AssetVersion, row.id, populate_existing=True)
    if existing is not None:
        return existing
    kind = "SCRIPT" if isinstance(row, Script) else "STORYBOARD"
    asset = content_asset(db, episode, kind)
    inputs = [
        {"source_asset_version_id": identity, "dependency_type": "STYLE_DEPENDENCY"}
        for identity in sorted(reference_ids(row.content_json))
    ]
    if isinstance(row, Storyboard):
        script = db.get(Script, row.script_version_id)
        assert script is not None
        register_content(db, script, actor)
        inputs.append(
            {"source_asset_version_id": str(script.id), "dependency_type": "CONTENT_DEPENDENCY"}
        )
    manifest = None
    if inputs:
        manifest, _ = capture_manifest(
            db,
            episode.project_id,
            actor,
            ManifestCreate.model_validate(
                {
                    "creation_key": uuid5(row.id, "original-content-inputs"),
                    "target_asset_id": asset.id,
                    "inputs": inputs,
                    "context": {"resource_type": kind, "resource_id": str(row.id)},
                }
            ),
        )
    registry = AssetVersion(
        id=row.id,
        asset_id=asset.id,
        version_no=row.version_no,
        object_key=f"database:{kind.lower()}:{row.id}",
        checksum=hashlib.sha256(json.dumps(row.content_json, sort_keys=True).encode()).hexdigest(),
        created_by=actor,
        status=row.status,
        metadata_json={
            "episode_id": str(episode.id),
            "content_registry": True,
            "dependency_manifest_id": str(manifest.id) if manifest else None,
        },
    )
    db.add(registry)
    db.flush()
    if manifest:
        attach_output(db, episode.project_id, manifest.id, registry)
    if row.run_id is not None:
        from arclight.application.production_versions import bind_output

        bind_output(db, episode.project_id, "WORKFLOW", row.run_id, kind, row.id)
    return registry


def content_changes(before: dict, after: dict, kind: str) -> list[tuple[str, list[str]]]:
    before, after = read_content(before), read_content(after)
    if before == after:
        return []
    if kind == "SCRIPT":

        def parts(value):
            value = json.loads(json.dumps(value))
            lines = {}
            for scene in value["scenes"]:
                for index, line in enumerate(scene.pop("dialogue", [])):
                    lines[line["line_id"]] = (scene["scene_id"], index, line)
            return value, lines

        old, old_lines = parts(before)
        new, new_lines = parts(after)
        if old == new:
            changed = sorted(
                key
                for key in old_lines.keys() | new_lines.keys()
                if old_lines.get(key) != new_lines.get(key)
            )
            return [("DIALOGUE", ["dialogue:" + key for key in changed])]
    if kind == "STORYBOARD":

        def unordered(value):
            return sorted(
                json.dumps({k: v for k, v in shot.items() if k != "shot_no"}, sort_keys=True)
                for shot in value["shots"]
            )

        if {k: v for k, v in before.items() if k != "shots"} == {
            k: v for k, v in after.items() if k != "shots"
        } and unordered(before) == unordered(after):
            return [("SEQUENCE", [])]
    return [("CONTENT", [])]


def approval_changes(db: Session, row: Script | Storyboard) -> list[dict]:
    """Read the proposed approval without registering or changing historical rows."""
    episode = db.get(Episode, row.episode_id)
    assert episode is not None
    kind = "SCRIPT" if isinstance(row, Script) else "STORYBOARD"
    asset = db.get(VisualAsset, uuid5(episode.id, "dependency:" + kind))
    model = Script if isinstance(row, Script) else Storyboard
    previous = cast(
        Script | Storyboard | None,
        db.get(model, asset.current_version_id)
        if asset and asset.current_version_id
        else db.scalar(
            select(model)
            .where(
                model.episode_id == row.episode_id, model.id != row.id, model.status == "APPROVED"
            )
            .order_by(model.version_no.desc())
            .limit(1)
        ),
    )
    if previous is None or previous.id == row.id:
        return []
    registry = db.get(AssetVersion, previous.id)
    # A legacy text without a registry cannot have outgoing version edges.
    if registry is None:
        return []
    return [
        {
            "source_asset_version_id": str(previous.id),
            "expected_revision": registry.revision,
            "change_kind": kind,
            "selectors": selectors,
        }
        for kind, selectors in content_changes(previous.content_json, row.content_json, kind)
    ]


def review_registry(
    db: Session, row: Script | Storyboard, actor: str, impact_digest: str | None = None
) -> None:
    episode = db.get(Episode, row.episode_id)
    assert episode is not None
    if row.status == "APPROVED":
        check_impact(db, episode.project_id, approval_changes(db, row), impact_digest)
    registry = register_content(db, row, actor)
    asset = db.get(VisualAsset, registry.asset_id)
    assert asset is not None
    if row.status == "APPROVED":
        if registry.stale:
            raise DomainError("STALE_DEPENDENCY", "输入已过期，请另存新版本后重新审核", 409)
        previous_id = asset.current_version_id
        propagation_id = None
        if previous_id is None:
            model = Script if isinstance(row, Script) else Storyboard
            previous = cast(
                Script | Storyboard | None,
                db.scalar(
                    select(model)
                    .where(
                        model.episode_id == row.episode_id,
                        model.id != row.id,
                        model.status == "APPROVED",
                    )
                    .order_by(model.version_no.desc())
                    .limit(1)
                ),
            )
            if previous:
                previous_id = register_content(db, previous, actor).id
        if previous_id and previous_id != row.id:
            model = Script if isinstance(row, Script) else Storyboard
            previous = cast(Script | Storyboard | None, db.get(model, previous_id))
            if previous:
                old_registry = db.get(AssetVersion, previous_id)
                assert old_registry is not None
                changes = [
                    {
                        "source_asset_version_id": str(previous_id),
                        "expected_revision": old_registry.revision,
                        "change_kind": change,
                        "selectors": selectors,
                    }
                    for change, selectors in content_changes(
                        previous.content_json, row.content_json, asset.asset_type
                    )
                ]
                if changes:
                    propagation, _ = invalidate(
                        db,
                        episode.project_id,
                        actor,
                        uuid5(row.id, "approved-content"),
                        changes,
                        "已批准新的精确文本版本",
                    )
                    propagation_id = str(propagation.id)
        asset.current_version_id = row.id
        asset.revision += 1
        audit(
            db,
            episode.project_id,
            actor,
            "asset.version_selected",
            asset.id,
            {
                "previous_version_id": str(previous_id) if previous_id else None,
                "version_id": str(row.id),
                "revision": asset.revision,
                "trigger": "TEXT_APPROVAL",
                "dependency_invalidation_id": propagation_id,
            },
        )
    registry.status = row.status if not registry.stale else registry.status
    registry.revision += 1
    db.flush()
