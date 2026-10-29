"""Explicit replacement inputs for a new candidate; never rebind an old output."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from arclight.application.dependencies import (
    capture_manifest,
    exact_version,
    lock_dependency_graph,
    require_manifest,
)
from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate
from arclight.persistence.models import AssetVersion, DependencyManifest


class RebuildInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    replacements: dict[UUID, UUID] = Field(default_factory=dict, max_length=64)


def original_manifest(db: Session, project_id: UUID, version: AssetVersion) -> DependencyManifest:
    identity = version.metadata_json.get("dependency_manifest_id")
    if not identity:
        raise DomainError("DEPENDENCY_MANIFEST_REQUIRED", "此版本没有可重建的输入清单", 409)
    return require_manifest(db, project_id, UUID(identity))


def rebuild_manifest(
    db: Session, project_id: UUID, version: AssetVersion, actor: str, payload: RebuildInputs
):
    lock_dependency_graph(db, project_id)
    version, _ = exact_version(db, project_id, version.id)
    if version.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "原版本状态已改变，请刷新后重试", 409)
    original = original_manifest(db, project_id, version)
    identities = {UUID(x["source_asset_version_id"]) for x in original.snapshot_json["inputs"]}
    if set(payload.replacements) - identities:
        raise DomainError("INVALID_REBUILD_INPUT", "替换项必须来自原始输入清单", 422)
    inputs = []
    for item in original.snapshot_json["inputs"]:
        old_id = UUID(item["source_asset_version_id"])
        replacement = payload.replacements.get(old_id, old_id)
        source, asset = exact_version(db, project_id, replacement)
        if str(asset.id) != item["asset_id"]:
            raise DomainError("INVALID_REBUILD_INPUT", "替换版本必须属于原输入资产", 422)
        inputs.append(
            {
                "source_asset_version_id": str(source.id),
                "dependency_type": item["dependency_type"],
                "selectors": item.get("selectors", []),
            }
        )
    return capture_manifest(
        db,
        project_id,
        actor,
        ManifestCreate.model_validate(
            {
                "creation_key": payload.creation_key,
                "target_asset_id": version.asset_id,
                "inputs": inputs,
                "context": {
                    "rebuild_from_version_id": str(version.id),
                    "previous_manifest_id": str(original.id),
                    "original_context": original.snapshot_json["context"],
                },
            }
        ),
    )
