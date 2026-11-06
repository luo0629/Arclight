"""Directory baselines and authorized exact images, using existing asset and object gates."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import get_args
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference
from arclight.application.art_proposal_store import asset_content
from arclight.application.art_proposals import asset_baseline
from arclight.application.content_references import require_asset
from arclight.application.context_builder import resolve_assets
from arclight.application.context_sources import asset_sources, verify_images
from arclight.application.context_sources import invalid as invalid_reference
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import AssetKind
from arclight.domain.context import ContextLimits
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtApprovedVersion
from arclight.persistence.models import AssetVersion, StoredFile, VisualAsset

DIRECTORY_KINDS = frozenset({"CHARACTER", "SCENE", "PROP", "STYLE"})


def scoped_directory(db: Session, project_id: UUID, asset_id: UUID) -> VisualAsset | None:
    asset = db.get(VisualAsset, asset_id, populate_existing=True)
    if asset is not None and asset.project_id != project_id:
        raise DomainError("REFERENCE_SCOPE_INVALID", "引用不在当前项目范围", 404)
    return asset


def usable_directory(asset: VisualAsset) -> bool:
    return asset.status not in {"ARCHIVED", "REJECTED", "STALE"}


def allowed_for_episode(asset: VisualAsset, episode_id: UUID) -> bool:
    scope = asset.metadata_json.get("usage_scope", {})
    return scope.get("kind", "PROJECT") == "PROJECT" or str(episode_id) in scope.get(
        "episode_ids", []
    )


def directory_snapshot(db: Session, asset: VisualAsset, episode_id: UUID) -> dict:
    """A formal setting without an image remains a directory, never a fabricated media version."""
    content = deepcopy(asset_content(asset))
    approved = db.scalar(
        select(ArtApprovedVersion)
        .where(ArtApprovedVersion.asset_id == asset.id)
        .order_by(ArtApprovedVersion.version_no.desc())
        .limit(1)
    )
    references = []
    profile = asset.character_profile or {}
    for image in profile.get("images", []):
        references.append(
            {
                **deepcopy(image),
                "is_standard": image["id"] == profile.get("standard_image_id"),
            }
        )
    scene_reference = (asset.scene_profile or {}).get("reference")
    if scene_reference:
        references.append({"kind": "SCENE_REFERENCE", "source": deepcopy(scene_reference)})
    if asset.current_version_id is not None:
        references.append(
            {
                "kind": "SELECTED_VERSION",
                "source": {
                    "asset_id": str(asset.id),
                    "asset_version_id": str(asset.current_version_id),
                },
            }
        )
    return {
        "directory": content,
        "baseline": asset_baseline(asset).model_dump(mode="json"),
        "approved_design": {
            "version_id": str(approved.id),
            "version_no": approved.version_no,
            "content_sha256": approved.content_sha256,
            "matches_directory": approved.content_sha256 == digest(content),
        }
        if approved
        else None,
        "image_candidates": references,
        "allowed_for_episode": allowed_for_episode(asset, episode_id),
    }


@dataclass(frozen=True)
class ReferenceRead:
    content: dict | None = None
    reason: str | None = None
    assets: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    image_file_ids: list[UUID] = field(default_factory=list)


def read_reference(
    db: Session,
    settings: Settings,
    project_id: UUID,
    episode_id: UUID,
    version_id: UUID,
    limits: ContextLimits,
    *,
    authorization_only: bool = False,
    include_images: bool = False,
) -> ReferenceRead:
    version = db.get(AssetVersion, version_id, populate_existing=True)
    asset = scoped_directory(db, project_id, version.asset_id) if version else None
    if version is None or asset is None:
        return ReferenceRead(reason="NOT_FOUND")
    if asset.asset_type not in get_args(AssetKind):
        raise DomainError("ART_REFERENCE_INVALID", "美术参考须为视觉资产", 422)
    try:
        require_asset(
            db,
            project_id,
            AssetReference(asset_id=str(asset.id), asset_version_id=str(version.id)),
            set(get_args(AssetKind)),
        )
    except DomainError as error:
        if error.code != "ASSET_NOT_USABLE":
            raise
        return ReferenceRead(reason="INPUT_NOT_USABLE")
    if not allowed_for_episode(asset, episode_id):
        return ReferenceRead(reason="OUT_OF_SCOPE")
    stored = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if stored is not None and (
        stored.project_id != project_id or stored.checksum != version.checksum
    ):
        invalid_reference()
    media = version.metadata_json.get("media", {})
    confirmed = bool(stored and stored.rights_confirmed and media.get("rights_confirmed") is True)
    content = {
        "asset_id": str(asset.id),
        "asset_version_id": str(version.id),
        "version_no": version.version_no,
        "checksum": version.checksum,
        "metadata_sha256": digest(version.metadata_json),
        "project_access": "AUTHORIZED",
        "usage_scope": deepcopy(asset.metadata_json.get("usage_scope", {"kind": "PROJECT"})),
        "rights_status": "CONFIRMED" if confirmed else "UNDECLARED",
        "rights_note": stored.rights_note if stored else None,
        "source_note": stored.source_note if stored else None,
        "provenance": deepcopy(version.metadata_json.get("provenance")),
    }
    if stored is None or stored.media_type not in {"image/png", "image/jpeg"}:
        return ReferenceRead(content=content, reason="NO_IMAGE")
    if not confirmed:
        return ReferenceRead(content=content, reason="RIGHTS_UNDECLARED")
    if authorization_only:
        return ReferenceRead(content=content)
    # Only explicitly selected images enter evidence. Full profile links remain in
    # directory materials, so multi-angle discovery never implicitly consumes a batch.
    assets = resolve_assets(
        db, project_id, {str(version.id)}, limits, follow_profile_references=False
    )
    sources = asset_sources(db, project_id, assets)
    try:
        verify_images(db, settings, project_id, sources)
    except DomainError as error:
        if error.code != "ASSET_OBJECT_UNAVAILABLE":
            raise
        return ReferenceRead(content=content, reason="OBJECT_UNAVAILABLE")
    reference = sources[0]["object_ref"]
    if reference is None:
        return ReferenceRead(content=content, reason="NO_IMAGE")
    content.update(asset=assets[0], image=reference)
    return ReferenceRead(
        content=content,
        assets=assets,
        sources=sources,
        image_file_ids=[UUID(reference["file_id"])] if include_images else [],
    )
