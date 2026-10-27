"""Project and lifecycle validation for visual profile references."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference
from arclight.application.content_references import require_asset
from arclight.core.errors import DomainError
from arclight.domain.asset_profiles import CharacterProfile, PropProfile, SceneProfile
from arclight.persistence.models import AssetVersion, VisualAsset


def validate_scene_profile(
    db: Session, project_id: UUID, asset: VisualAsset, profile: SceneProfile
) -> None:
    if profile.reference is not None:
        if profile.reference.asset_id != asset.id:
            raise DomainError("INVALID_ASSET_REFERENCE", "场景参考必须属于当前场景的精确版本", 422)
        require_asset(
            db, project_id, AssetReference(**profile.reference.model_dump(mode="json")), {"SCENE"}
        )


def validate_prop_profile(db: Session, project_id: UUID, profile: PropProfile) -> None:
    if profile.holder is not None:
        require_asset(
            db, project_id, AssetReference(**profile.holder.model_dump(mode="json")), {"CHARACTER"}
        )


def validate_character_profile(
    db: Session, project_id: UUID, asset: VisualAsset, profile: CharacterProfile
) -> None:
    for image in profile.images:
        version = db.get(AssetVersion, image.source.asset_version_id)
        owner = db.get(VisualAsset, image.source.asset_id)
        if (
            version is None
            or owner is None
            or version.asset_id != owner.id
            or owner.project_id != project_id
            or owner.asset_type not in {"CHARACTER", "IMAGE", "SHOT_IMAGE"}
            or (owner.asset_type == "CHARACTER" and owner.id != asset.id)
        ):
            raise DomainError(
                "INVALID_ASSET_REFERENCE", "参考图必须是本角色或本项目图片的精确版本", 422
            )
        if (
            version.stale
            or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
            or owner.status in {"ARCHIVED", "REJECTED", "STALE"}
        ):
            raise DomainError("ASSET_NOT_USABLE", "参考图已拒绝、失效或归档", 422)
        if image.id == profile.standard_image_id and version.status not in {"APPROVED", "LOCKED"}:
            raise DomainError(
                "STANDARD_IMAGE_NOT_APPROVED", "标准参考图必须人工选择已批准的精确版本", 422
            )
