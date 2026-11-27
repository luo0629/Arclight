"""One active generation per registered target, with frozen selection expectations."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContentV3, read_shot_content
from arclight.core.errors import DomainError
from arclight.domain.generation import ACTIVE_STATUSES
from arclight.domain.generation_requests import ShotReviewBinding
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import DependencyManifest, Episode, Shot, Storyboard


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


def candidate_episode_ids(
    db: Session, project_id: UUID, version_ids: set[UUID]
) -> dict[UUID, UUID]:
    if not version_ids:
        return {}

    bound: dict[UUID, tuple[UUID | None, ShotReviewBinding]] = {}
    direct: dict[UUID, UUID] = {}
    # A replacement or restored video can have a new candidate manifest. Its job
    # still identifies the original generation and frozen shot binding.
    for version_id, episode_id, raw_binding in db.execute(
        select(
            GenerationCandidate.asset_version_id,
            GenerationJob.episode_id,
            DependencyManifest.snapshot_json["context"]["continuity_review"],
        )
        .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .where(
            GenerationCandidate.asset_version_id.in_(version_ids),
            GenerationCandidate.project_id == project_id,
            GenerationJob.project_id == project_id,
            GenerationJob.task_type.in_(["IMAGE", "VIDEO"]),
            DependencyManifest.project_id == project_id,
        )
    ):
        if raw_binding is not None:
            try:
                bound[version_id] = (episode_id, ShotReviewBinding.model_validate(raw_binding))
            except ValidationError:
                continue
        elif episode_id is not None:
            direct[version_id] = episode_id

    project_episodes = (
        set(
            db.scalars(
                select(Episode.id).where(
                    Episode.project_id == project_id, Episode.id.in_(set(direct.values()))
                )
            )
        )
        if direct
        else set()
    )
    result = {
        version_id: episode_id
        for version_id, episode_id in direct.items()
        if episode_id in project_episodes
    }
    if bound:
        targets = {
            shot_id: (board_id, episode_id)
            for shot_id, board_id, episode_id in db.execute(
                select(Shot.id, Storyboard.id, Episode.id)
                .join(Storyboard, Storyboard.id == Shot.storyboard_id)
                .join(Episode, Episode.id == Storyboard.episode_id)
                .where(
                    Episode.project_id == project_id,
                    Shot.id.in_({binding.shot_id for _, binding in bound.values()}),
                )
            )
        }
        for version_id, (episode_id, binding) in bound.items():
            target = targets.get(binding.shot_id)
            if (
                target is not None
                and target[0] == binding.storyboard_id
                and (episode_id is None or target[1] == episode_id)
            ):
                result[version_id] = target[1]
    return result
