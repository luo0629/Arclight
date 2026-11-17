"""One active generation per registered target, with frozen selection expectations."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContentV3, read_shot_content
from arclight.core.errors import DomainError
from arclight.domain.generation import ACTIVE_STATUSES
from arclight.persistence.generation import GenerationJob


def require_available_target(
    db: Session, asset_id: UUID | None, *, exclude_job_id: UUID | None = None
) -> None:
    if asset_id is None:
        return
    statement = select(GenerationJob.id).where(
        GenerationJob.target_asset_id == asset_id, GenerationJob.status.in_(ACTIVE_STATUSES)
    )
    if exclude_job_id is not None:
        statement = statement.where(GenerationJob.id != exclude_job_id)
    if db.scalar(statement.limit(1)) is not None:
        raise DomainError(
            "GENERATION_TARGET_BUSY", "此资产已有未结束的生成任务，请先完成或确认取消", 409
        )


def require_available_shot(
    db: Session,
    project_id: UUID,
    binding: dict,
    episode_id: UUID | None,
    *,
    exclude_job_id: UUID | None = None,
    task_type: str | None = None,
) -> None:
    """Use the existing project write gate before checking an exact shot target."""
    from arclight.application.continuity_workflow import require_shot_binding
    from arclight.application.dependencies import exact_version
    from arclight.domain.generation_requests import ShotReviewBinding
    from arclight.persistence.models import DependencyManifest, Shot

    target = ShotReviewBinding.model_validate(binding)
    require_shot_binding(db, project_id, target, episode_id)
    version, source = exact_version(db, project_id, target.storyboard_id)
    if version.stale or version.status not in {"APPROVED", "LOCKED"} or source.status == "ARCHIVED":
        raise DomainError("ASSET_NOT_USABLE", "分镜来源已失效或不可用于生产", 409)
    shot = db.get(Shot, target.shot_id, populate_existing=True)
    assert shot is not None
    if task_type == "IMAGE" and shot.content_json.get("schema_version") == "3.0":
        try:
            content = read_shot_content(shot.content_json)
        except ValidationError as exc:
            raise DomainError("INVALID_STORYBOARD", "表演镜头内容不符合当前合同", 409) from exc
        if (
            isinstance(content, ShotContentV3)
            and content.performance.motion_requirement == "dynamic_required"
        ):
            raise DomainError(
                "DYNAMIC_MEDIA_REQUIRED",
                "必须动态的镜头不能进入镜头图生产，请修订制作路线或转人工决定。",
                409,
            )
    if task_type is not None and shot.content_json.get("schema_version") == "3.0":
        from arclight.application.text_reviews import require_performance_production

        require_performance_production(db, shot.storyboard_id)
    selected = shot.content_json.get("selected_visual_asset")
    if selected is not None:
        selected_version, selected_asset = exact_version(
            db, project_id, UUID(selected["asset_version_id"])
        )
        if (
            str(selected_asset.id) != selected["asset_id"]
            or selected_asset.status in {"LOCKED", "ARCHIVED"}
            or selected_version.status in {"LOCKED", "ARCHIVED"}
        ):
            raise DomainError("ASSET_READ_ONLY", "此镜头选用素材已锁定或归档，请先处理原素材", 409)
    statement = (
        select(GenerationJob.id)
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .where(
            GenerationJob.project_id == project_id,
            GenerationJob.status.in_(ACTIVE_STATUSES),
            DependencyManifest.snapshot_json["context"]["continuity_review"][
                "storyboard_id"
            ].as_string()
            == str(target.storyboard_id),
            DependencyManifest.snapshot_json["context"]["continuity_review"]["shot_id"].as_string()
            == str(target.shot_id),
        )
    )
    if exclude_job_id is not None:
        statement = statement.where(GenerationJob.id != exclude_job_id)
    if db.scalar(statement.limit(1)) is not None:
        raise DomainError("GENERATION_SHOT_BUSY", "此镜头已有未结束的任务，未知结果须先对账", 409)
