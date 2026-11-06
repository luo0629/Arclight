"""Read-only validation of proposed art content against existing project contracts."""

from typing import get_args
from uuid import UUID

from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference
from arclight.application.asset_profiles import (
    validate_character_profile,
    validate_prop_profile,
    validate_scene_profile,
)
from arclight.application.commands import require_project
from arclight.application.content_references import require_asset
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import (
    ArtProposal,
    AssetKind,
    CharacterConcept,
    ExistingAssetTarget,
    PropConcept,
    SceneConcept,
    VersionArtSource,
    VisualBibleConcept,
)
from arclight.domain.versions import digest
from arclight.domain.visual_bible import FIELD_NAMES
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Script,
    VisualAsset,
    VisualBibleVersion,
)


def asset_baseline(asset: VisualAsset) -> ExistingAssetTarget:
    """Directory revision and selection lock are distinct from an image version."""
    content = {
        "id": str(asset.id),
        "asset_type": asset.asset_type,
        "name": asset.name,
        "status": asset.status,
        "current_version_id": str(asset.current_version_id) if asset.current_version_id else None,
        "revision": asset.revision,
        "lock_version": asset.lock_version,
        "metadata": asset.metadata_json,
        "character_profile": asset.character_profile,
        "scene_profile": asset.scene_profile,
        "prop_profile": asset.prop_profile,
    }
    return ExistingAssetTarget(
        asset_id=asset.id,
        revision=asset.revision,
        lock_version=asset.lock_version,
        current_version_id=asset.current_version_id,
        content_sha256=digest(content),
    )


def visual_version_sha256(version: VisualBibleVersion) -> str:
    return digest(
        {
            "version_id": str(version.id),
            "version_no": version.version_no,
            "fields": {name: getattr(version, name) for name in FIELD_NAMES},
            "locked_fields": version.locked_fields,
        }
    )


def _asset_version(
    db: Session, project_id: UUID, version_id: UUID
) -> tuple[AssetVersion, VisualAsset]:
    version = db.get(AssetVersion, version_id, populate_existing=True)
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True) if version else None
    if version is None or asset is None or asset.project_id != project_id:
        raise DomainError("INVALID_ASSET_REFERENCE", "提案参考不存在或不属于当前项目", 422)
    if (
        version.stale
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
    ):
        raise DomainError("ASSET_NOT_USABLE", "提案参考已失效、拒绝或归档", 422)
    return version, asset


def _source(db: Session, proposal: ArtProposal, source: VersionArtSource) -> None:
    if source.resource_type == "ASSET_VERSION":
        version, owner = _asset_version(db, proposal.project_id, source.resource_id)
        if owner.asset_type not in get_args(AssetKind):
            raise DomainError("ART_SOURCE_INVALID", "视觉素材来源不能冒充其他内容类型", 422)
        checksum = version.checksum
    elif source.resource_type == "VISUAL_BIBLE":
        bible = db.get(VisualBibleVersion, source.resource_id)
        if bible is None or bible.project_id != proposal.project_id:
            raise DomainError("ART_SOURCE_INVALID", "视觉规范来源不存在或不属于当前项目", 422)
        checksum = visual_version_sha256(bible)
    else:
        script = db.get(Script, source.resource_id, populate_existing=True)
        episode = db.get(Episode, script.episode_id) if script else None
        if (
            script is None
            or episode is None
            or episode.project_id != proposal.project_id
            or (proposal.episode_id is not None and proposal.episode_id != episode.id)
        ):
            raise DomainError("ART_SOURCE_INVALID", "剧本来源不存在或不属于当前提案范围", 422)
        if script.status != "APPROVED":
            raise DomainError("ART_SOURCE_INVALID", "美术提案须使用已批准的精确剧本", 422)
        version, _ = _asset_version(db, proposal.project_id, script.id)
        if version.status not in {"APPROVED", "LOCKED"}:
            raise DomainError("ART_SOURCE_INVALID", "剧本资产版本须保持已批准", 422)
        checksum = digest(script.content_json)
    if source.content_sha256 != checksum:
        raise DomainError("ART_SOURCE_CHANGED", "提案来源哈希与精确版本不一致", 409)


def validate_art_proposal(db: Session, proposal: ArtProposal, actor: str) -> None:
    """Validate a submission; approval, locking and persistence belong to later services."""
    proposal = ArtProposal.model_validate(proposal.model_dump(mode="json"))
    project = require_project(db, proposal.project_id, actor)
    if project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目不能提交新美术提案", 409)
    if proposal.episode_id is not None:
        episode = db.get(Episode, proposal.episode_id)
        if episode is None or episode.project_id != project.id:
            raise DomainError("ART_SCOPE_INVALID", "提案剧集不属于当前项目", 422)
        if episode.status == "ARCHIVED":
            raise DomainError("EPISODE_ARCHIVED", "归档剧集不能提交新美术提案", 409)
    for source in proposal.sources:
        if isinstance(source, VersionArtSource):
            _source(db, proposal, source)
    for reference in proposal.references:
        version, asset = _asset_version(db, project.id, reference.asset.asset_version_id)
        if asset.id != reference.asset.asset_id or asset.asset_type != reference.asset_type:
            raise DomainError("INVALID_ASSET_REFERENCE", "提案参考身份或类型与精确版本不匹配", 422)
        if reference.usage == "APPROVED_REFERENCE":
            require_asset(
                db,
                project.id,
                AssetReference(**reference.asset.model_dump(mode="json")),
                {reference.asset_type},
            )
    for option in proposal.options:
        for design in option.designs:
            if isinstance(design, VisualBibleConcept):
                current = snapshot(db, project)
                if (
                    current["version_id"] != (str(design.version_id) if design.version_id else None)
                    or current["version_no"] != design.version_no
                    or digest(current) != design.content_sha256
                ):
                    raise DomainError("ART_PROPOSAL_BASE_STALE", "视觉规范提案基准已变化", 409)
                continue
            target = design.target
            target_asset: VisualAsset | None = None
            if isinstance(target, ExistingAssetTarget):
                target_asset = db.get(VisualAsset, target.asset_id, populate_existing=True)
                if (
                    target_asset is None
                    or target_asset.project_id != project.id
                    or target_asset.asset_type != design.kind
                ):
                    raise DomainError("ART_TARGET_INVALID", "提案目标不存在或类型／项目不匹配", 422)
                if target_asset.status in {"ARCHIVED", "REJECTED", "STALE"}:
                    raise DomainError("ASSET_NOT_USABLE", "提案目标已失效、拒绝或归档", 422)
                if target != asset_baseline(target_asset):
                    raise DomainError("ART_PROPOSAL_BASE_STALE", "资产提案基准或锁版本已变化", 409)
            if design.metadata:
                for episode_id in design.metadata.usage_scope.episode_ids:
                    episode = db.get(Episode, episode_id)
                    if episode is None or episode.project_id != project.id:
                        raise DomainError(
                            "INVALID_ASSET_SCOPE", "提案使用范围只能包含本项目剧集", 422
                        )
            if isinstance(design, CharacterConcept) and design.profile:
                if target_asset is not None:
                    validate_character_profile(db, project.id, target_asset, design.profile)
                else:
                    for image in design.profile.images:
                        version, owner = _asset_version(
                            db, project.id, image.source.asset_version_id
                        )
                        if owner.asset_type not in {"IMAGE", "SHOT_IMAGE"}:
                            raise DomainError(
                                "INVALID_ASSET_REFERENCE", "新角色须使用图片参考", 422
                            )
                        if image.id == design.profile.standard_image_id and version.status not in {
                            "APPROVED",
                            "LOCKED",
                        }:
                            raise DomainError(
                                "STANDARD_IMAGE_NOT_APPROVED", "标准参考须已批准", 422
                            )
            elif isinstance(design, SceneConcept) and design.profile and target_asset is not None:
                validate_scene_profile(db, project.id, target_asset, design.profile)
            elif isinstance(design, PropConcept) and design.profile:
                validate_prop_profile(db, project.id, design.profile)
