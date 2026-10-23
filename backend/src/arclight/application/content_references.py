"""文本合同的权威引用校验；复用资产版本和通用审核记录，不创建影子资产。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference, ScriptContent, StoryboardContent
from arclight.core.errors import DomainError
from arclight.persistence.models import AssetVersion, Episode, Review, VisualAsset, WorkflowRun


def require_asset(
    db: Session, project_id: UUID, reference: AssetReference | None, kinds: set[str]
) -> None:
    if reference is None:
        return
    row = db.execute(
        select(AssetVersion, VisualAsset)
        .join(VisualAsset, AssetVersion.asset_id == VisualAsset.id)
        .where(AssetVersion.id == UUID(reference.asset_version_id))
        .with_for_update(key_share=True)
    ).first()
    # 无论不存在还是越权，均不泄露其他项目的资产信息。
    if row is None or row[1].project_id != project_id:
        raise DomainError("INVALID_ASSET_REFERENCE", "资产版本不存在或不属于当前项目", 422)
    version, asset = row
    if str(asset.id) != reference.asset_id or asset.asset_type not in kinds:
        raise DomainError("INVALID_ASSET_REFERENCE", "资产身份、类型与精确版本不匹配", 422)
    if (
        version.stale
        or version.status not in {"APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
    ):
        raise DomainError("ASSET_NOT_USABLE", "引用的资产版本未批准、已失效或已归档", 422)


def validate_script_assets(db: Session, project_id: UUID, content: ScriptContent) -> None:
    for item in content.character_assets:
        require_asset(db, project_id, item.asset, {"CHARACTER"})
    for scene in content.scenes:
        require_asset(db, project_id, scene.scene_asset, {"SCENE"})
        require_asset(db, project_id, scene.location_asset, {"LOCATION", "SCENE"})
        for prop in scene.props:
            require_asset(db, project_id, prop, {"PROP"})


def validate_storyboard_assets(db: Session, project_id: UUID, content: StoryboardContent) -> None:
    for shot in content.shots:
        for item in shot.character_details:
            require_asset(db, project_id, item.asset, {"CHARACTER"})
            require_asset(db, project_id, item.costume, {"COSTUME"})
        require_asset(db, project_id, shot.scene_asset, {"SCENE"})
        require_asset(db, project_id, shot.location_asset, {"LOCATION", "SCENE"})
        for prop in shot.props:
            require_asset(db, project_id, prop, {"PROP"})
        require_asset(db, project_id, shot.selected_visual_asset, {"IMAGE", "SHOT_IMAGE"})
        if shot.quality_review is not None:
            review = db.scalar(
                select(Review)
                .join(WorkflowRun, Review.run_id == WorkflowRun.id)
                .join(Episode, WorkflowRun.episode_id == Episode.id)
                .where(
                    Review.id == UUID(shot.quality_review.review_id),
                    Episode.project_id == project_id,
                    Review.resource_type == "asset_version",
                    Review.version_id == UUID(shot.quality_review.asset_version_id),
                    Review.decision == "APPROVE",
                )
                .with_for_update(key_share=True)
            )
            if review is None:
                raise DomainError(
                    "INVALID_QUALITY_REFERENCE", "质检引用不是当前素材版本的有效批准记录", 422
                )
