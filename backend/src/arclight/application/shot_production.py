"""Read existing GEN, asset lifecycle and QA state without granting new authority."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContent
from arclight.application.commands import require_episode, require_project
from arclight.application.continuity_selection import require_shot_review
from arclight.application.dependencies import exact_version
from arclight.application.generation_targets import require_available_shot
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.shot_production import (
    ProductionBlocker,
    ProductionCandidate,
    ProductionJob,
    ProductionShot,
    StoryboardProduction,
)
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationJob,
    Shot,
    Storyboard,
    VisualAsset,
)


def generation_request(
    db: Session, project_id: UUID, board: Storyboard, shot: Shot
) -> GenerationCreate:
    content = ShotContent.model_validate(shot.content_json)
    references = [content.scene_asset, content.location_asset, *content.props]
    for character in content.character_details:
        references.extend([character.asset, character.costume])
    inputs = [
        {
            "source_asset_version_id": str(board.id),
            "dependency_type": "CONTENT_DEPENDENCY",
            "selectors": [f"shot:{shot.id}"],
        }
    ]
    seen = {str(board.id)}
    for reference in references:
        if reference is not None and reference.asset_version_id not in seen:
            version, asset = exact_version(db, project_id, UUID(reference.asset_version_id))
            if str(asset.id) != reference.asset_id:
                raise DomainError("INVALID_ASSET_REFERENCE", "镜头参考的精确资产不一致", 409)
            if (
                version.stale
                or version.status not in {"APPROVED", "LOCKED"}
                or asset.status == "ARCHIVED"
            ):
                raise DomainError("ASSET_NOT_USABLE", "镜头参考已失效或尚未批准", 409)
            seen.add(reference.asset_version_id)
            inputs.append(
                {
                    "source_asset_version_id": reference.asset_version_id,
                    "dependency_type": "REFERENCE_DEPENDENCY",
                    "selectors": [],
                }
            )
    selected = content.selected_visual_asset
    target = {}
    if selected is not None:
        _, asset = exact_version(db, project_id, UUID(selected.asset_version_id))
        if asset.asset_type != "SHOT_IMAGE":
            raise DomainError("INVALID_GENERATION_TARGET", "当前选用素材须通过原素材入口处理", 409)
        target = {
            "target_asset_id": str(asset.id),
            "expected_asset": {
                "revision": asset.revision,
                "lock_version": asset.lock_version,
                "current_version_id": str(asset.current_version_id)
                if asset.current_version_id
                else None,
            },
        }
    return GenerationCreate.model_validate(
        {
            "episode_id": str(board.episode_id),
            "target_type": "SHOT_IMAGE",
            "task_type": "IMAGE",
            "scheduling_mode": "BATCH",
            "review_target": {"storyboard_id": str(board.id), "shot_id": str(shot.id)},
            "inputs": inputs,
            "spec": {
                "provider": "mock",
                "model": "mock-image-v1",
                "model_config_version": "shot-production-ui/1",
                "prompt": (
                    f"{content.visual_description}\n"
                    f"景别：{content.shot_size}；运镜：{content.camera_movement}\n"
                    f"构图：{content.composition}"
                ),
                "prompt_version": "shot-production-ui/1",
                "output_spec": {"width": 1024, "height": 1024, "format": "png"},
                "context": {},
            },
            **target,
        }
    )


def read_candidates(
    db: Session,
    project_id: UUID,
    shot_id: UUID,
    storyboard_id: UUID,
    selected_version_id: UUID | None,
    *,
    writable: bool,
    owner: bool,
) -> tuple[list[ProductionCandidate], bool]:
    rows = list(
        db.execute(
            select(GenerationCandidate, AssetVersion, VisualAsset)
            .join(AssetVersion, AssetVersion.id == GenerationCandidate.asset_version_id)
            .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
            .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
            .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
            .where(
                GenerationCandidate.project_id == project_id,
                or_(
                    and_(
                        DependencyManifest.snapshot_json["context"]["continuity_review"][
                            "storyboard_id"
                        ].as_string()
                        == str(storyboard_id),
                        DependencyManifest.snapshot_json["context"]["continuity_review"][
                            "shot_id"
                        ].as_string()
                        == str(shot_id),
                    ),
                    GenerationCandidate.asset_version_id == selected_version_id,
                ),
            )
            .order_by(GenerationCandidate.created_at.desc(), GenerationCandidate.id)
            .limit(17)
        )
    )
    candidates = []
    for candidate, version, asset in rows[:16]:
        blocker = None
        try:
            require_shot_review(db, version)
        except DomainError as exc:
            blocker = ProductionBlocker(code=exc.code, message=exc.message)
        editable = writable and asset.status not in {"LOCKED", "ARCHIVED"} and not version.stale
        usable = editable and blocker is None
        candidates.append(
            ProductionCandidate(
                id=candidate.id,
                asset_id=asset.id,
                version_id=version.id,
                version_no=version.version_no,
                revision=version.revision,
                status=version.status,
                image_path=f"/projects/{project_id}/assets/{asset.id}/versions/{version.id}/image",
                can_submit=usable and version.status == "DRAFT",
                can_approve=usable and owner and version.status == "READY_FOR_REVIEW",
                can_reject=editable and owner and version.status == "READY_FOR_REVIEW",
                blocker=blocker,
            )
        )
    return candidates, len(rows) > 16


def read_production(
    db: Session, episode_id: UUID, storyboard_id: UUID, actor: str, *, limit: int, offset: int
) -> StoryboardProduction:
    episode = require_episode(db, episode_id, actor)
    project = require_project(db, episode.project_id, actor)
    board = db.get(Storyboard, storyboard_id, populate_existing=True)
    if board is None or board.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "分镜版本不存在", 404)
    writable = True
    try:
        require_episode(db, episode_id, actor, write=True)
    except DomainError:
        writable = False
    owner = project.owner_id == actor
    total = (
        db.scalar(select(func.count()).select_from(Shot).where(Shot.storyboard_id == board.id)) or 0
    )
    shots = list(
        db.scalars(
            select(Shot)
            .where(Shot.storyboard_id == board.id)
            .order_by(Shot.shot_no)
            .offset(offset)
            .limit(limit)
        )
    )
    jobs: dict[str, GenerationJob] = {}
    # One ordered query for this page; only the authoritative frozen binding identifies a shot.
    for job, manifest in db.execute(
        select(GenerationJob, DependencyManifest)
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .where(
            GenerationJob.project_id == project.id,
            DependencyManifest.snapshot_json["context"]["continuity_review"][
                "storyboard_id"
            ].as_string()
            == str(board.id),
            DependencyManifest.snapshot_json["context"]["continuity_review"]["shot_id"]
            .as_string()
            .in_([str(shot.id) for shot in shots]),
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id)
    ):
        jobs.setdefault(manifest.snapshot_json["context"]["continuity_review"]["shot_id"], job)
    results = []
    for shot in shots:
        job = jobs.get(str(shot.id))
        blocker = None
        payload = None
        try:
            if not writable:
                require_episode(db, episode_id, actor, write=True)
            require_available_shot(
                db,
                project.id,
                {"storyboard_id": str(board.id), "shot_id": str(shot.id)},
                episode_id,
                exclude_job_id=job.id if job else None,
            )
            payload = generation_request(db, project.id, board, shot)
        except DomainError as exc:
            blocker = ProductionBlocker(code=exc.code, message=exc.message)
        except ValidationError:
            blocker = ProductionBlocker(
                code="SHOT_GENERATION_INPUT_INVALID",
                message="镜头内容或引用超出当前媒体输入合同，请核对精确来源。",
            )
        quote = (
            db.scalar(select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id))
            if job
            else None
        )
        selected = shot.content_json.get("selected_visual_asset")
        candidates, truncated = read_candidates(
            db,
            project.id,
            shot.id,
            board.id,
            UUID(selected["asset_version_id"]) if selected else None,
            writable=writable,
            owner=owner,
        )
        results.append(
            ProductionShot(
                id=shot.id,
                shot_no=shot.shot_no,
                description=shot.description,
                job=ProductionJob(
                    id=job.id,
                    status=job.status,
                    revision=job.revision,
                    attempt_count=job.attempt_count,
                    maximum_cost=str(quote.maximum_cost) if quote else None,
                    currency=quote.currency if quote else None,
                )
                if job
                else None,
                request=payload,
                blocker=blocker,
                candidates=candidates,
                candidates_truncated=truncated,
            )
        )
    return StoryboardProduction(
        project_id=project.id,
        episode_id=episode_id,
        storyboard_id=board.id,
        writable=writable,
        owner=owner,
        total=total,
        offset=offset,
        next_offset=offset + limit if offset + len(shots) < total else None,
        shots=results,
    )
