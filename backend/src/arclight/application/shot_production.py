"""Read existing GEN, asset lifecycle and QA state without granting new authority."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference, ShotContentV3, read_shot_content
from arclight.application.commands import require_episode, require_project
from arclight.application.continuity_neighbors import selected_neighbor
from arclight.application.continuity_selection import require_shot_review, validate_selected_shot
from arclight.application.dependencies import exact_version
from arclight.application.generation_targets import candidate_episode_ids, require_available_shot
from arclight.application.video_review import require_video_review
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.shot_production import (
    ProductionBlocker,
    ProductionCandidate,
    ProductionJob,
    ProductionShot,
    ProductionVideoCandidate,
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
    content = read_shot_content(shot.content_json)
    if (
        isinstance(content, ShotContentV3)
        and content.performance.motion_requirement == "dynamic_required"
    ):
        raise DomainError(
            "DYNAMIC_MEDIA_REQUIRED",
            "此镜头必须使用动态素材；当前镜头图入口不能以静图代替，请修订制作路线或等待视频生产接入。",
            409,
        )
    if isinstance(content, ShotContentV3):
        from arclight.application.text_reviews import require_performance_production

        require_performance_production(db, board.id)
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


def selected_image_version_id(
    db: Session, project_id: UUID, episode_id: UUID, shot: Shot
) -> UUID | None:
    try:
        content = read_shot_content(shot.content_json)
        reference = content.selected_visual_asset
        if reference is None:
            selected = selected_neighbor(db, project_id, shot.id)
            if selected is None:
                return None
            version, asset = exact_version(db, project_id, UUID(selected["asset_version_id"]))
            return version.id if asset.asset_type in {"IMAGE", "SHOT_IMAGE"} else None

        version, asset = exact_version(db, project_id, UUID(reference.asset_version_id))
        if (
            asset.asset_type not in {"IMAGE", "SHOT_IMAGE"}
            or str(asset.id) != reference.asset_id
            or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
            or asset.current_version_id != version.id
            or version.status not in {"APPROVED", "LOCKED"}
            or version.stale
        ):
            return None
        record = require_shot_review(db, version)
        validate_selected_shot(db, record, content, episode_id)
        if record.review.target.shot_id != shot.id and (
            content.quality_review is None
            or content.quality_review.review_id != str(record.review.result_id)
        ):
            return None
    except (DomainError, ValidationError):
        return None
    return version.id


def read_candidates(
    db: Session,
    project_id: UUID,
    episode_id: UUID,
    shot_id: UUID,
    storyboard_id: UUID,
    selected_version_id: UUID | None,
    *,
    recorded_reference: AssetReference | None,
    writable: bool,
    owner: bool,
) -> tuple[list[ProductionCandidate], bool]:
    exact_versions = [GenerationCandidate.asset_version_id == selected_version_id]
    if recorded_reference is not None:
        version_id = UUID(recorded_reference.asset_version_id)
        if candidate_episode_ids(db, project_id, {version_id}).get(version_id) == episode_id:
            exact_versions.append(
                and_(
                    GenerationCandidate.asset_version_id == version_id,
                    VisualAsset.id == UUID(recorded_reference.asset_id),
                )
            )
    rows = list(
        db.execute(
            select(GenerationCandidate, AssetVersion, VisualAsset)
            .join(AssetVersion, AssetVersion.id == GenerationCandidate.asset_version_id)
            .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
            .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
            .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
            .where(
                GenerationCandidate.project_id == project_id,
                VisualAsset.project_id == project_id,
                VisualAsset.asset_type.in_(["IMAGE", "SHOT_IMAGE"]),
                GenerationJob.project_id == project_id,
                DependencyManifest.project_id == project_id,
                or_(
                    and_(
                        or_(
                            GenerationJob.episode_id == episode_id,
                            GenerationJob.episode_id.is_(None),
                        ),
                        DependencyManifest.snapshot_json["context"]["continuity_review"][
                            "storyboard_id"
                        ].as_string()
                        == str(storyboard_id),
                        DependencyManifest.snapshot_json["context"]["continuity_review"][
                            "shot_id"
                        ].as_string()
                        == str(shot_id),
                    ),
                    *exact_versions,
                ),
            )
            .order_by(
                case((GenerationCandidate.asset_version_id == selected_version_id, 0), else_=1),
                GenerationCandidate.created_at.desc(),
                GenerationCandidate.id,
            )
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


def read_video_candidates(
    db: Session, project_id: UUID, episode_id: UUID, storyboard_id: UUID, shot_id: UUID
) -> tuple[list[ProductionVideoCandidate], bool]:
    current = and_(
        VisualAsset.current_version_id == AssetVersion.id,
        VisualAsset.status.not_in(["ARCHIVED", "STALE", "REJECTED"]),
        AssetVersion.status.in_(["APPROVED", "LOCKED"]),
        AssetVersion.stale.is_(False),
    )
    rows = list(
        db.execute(
            select(GenerationCandidate, AssetVersion, VisualAsset, current.label("current"))
            .join(AssetVersion, AssetVersion.id == GenerationCandidate.asset_version_id)
            .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
            .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
            .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
            .where(
                GenerationCandidate.project_id == project_id,
                VisualAsset.project_id == project_id,
                VisualAsset.asset_type == "VIDEO",
                GenerationJob.project_id == project_id,
                GenerationJob.target_type == "VIDEO",
                or_(GenerationJob.episode_id == episode_id, GenerationJob.episode_id.is_(None)),
                DependencyManifest.project_id == project_id,
                DependencyManifest.snapshot_json["context"]["continuity_review"][
                    "storyboard_id"
                ].as_string()
                == str(storyboard_id),
                DependencyManifest.snapshot_json["context"]["continuity_review"][
                    "shot_id"
                ].as_string()
                == str(shot_id),
            )
            .order_by(
                case((current, 0), else_=1),
                GenerationCandidate.created_at.desc(),
                GenerationCandidate.id,
            )
            .limit(17)
        )
    )
    videos = []
    for candidate, version, asset, current_version in rows[:16]:
        selected = False
        if current_version:
            try:
                report = require_video_review(db, version).report
                selected = (
                    report.episode_id == episode_id
                    and report.target.storyboard_id == storyboard_id
                    and report.target.shot_id == shot_id
                )
            except DomainError:
                pass
        videos.append(
            ProductionVideoCandidate(
                id=candidate.id,
                job_id=candidate.job_id,
                asset_id=asset.id,
                version_id=version.id,
                version_no=version.version_no,
                revision=version.revision,
                asset_revision=asset.revision,
                status=version.status,
                content_path=f"/projects/{project_id}/generation-candidates/{candidate.id}/content",
                selected=selected,
            )
        )
    return videos, len(rows) > 16


def read_production(
    db: Session,
    episode_id: UUID,
    storyboard_id: UUID,
    actor: str,
    *,
    limit: int,
    offset: int,
    shot_id: UUID | None = None,
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
    scope = Shot.storyboard_id == board.id
    query = select(Shot).where(scope)
    if shot_id is None:
        total = db.scalar(select(func.count()).select_from(Shot).where(scope)) or 0
        shots = list(db.scalars(query.order_by(Shot.shot_no).offset(offset).limit(limit)))
    else:
        shot = db.scalar(query.where(Shot.id == shot_id))
        if shot is None:
            raise DomainError("NOT_FOUND", "镜头不存在", 404)
        shots = [shot]
        total = 1
        offset = 0
    jobs: dict[str, GenerationJob] = {}
    # One ordered query for this page; only the authoritative frozen binding identifies a shot.
    for job, manifest in db.execute(
        select(GenerationJob, DependencyManifest)
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .where(
            GenerationJob.project_id == project.id,
            GenerationJob.target_type == "SHOT_IMAGE",
            or_(GenerationJob.episode_id == episode_id, GenerationJob.episode_id.is_(None)),
            DependencyManifest.project_id == project.id,
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
        reference = None
        try:
            raw_reference = shot.content_json.get("selected_visual_asset")
            if raw_reference is not None:
                reference = AssetReference.model_validate(raw_reference)
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
        selected_version_id = selected_image_version_id(db, project.id, episode_id, shot)
        candidates, truncated = read_candidates(
            db,
            project.id,
            episode_id,
            shot.id,
            board.id,
            selected_version_id,
            recorded_reference=reference if selected_version_id is None else None,
            writable=writable,
            owner=owner,
        )
        videos, videos_truncated = read_video_candidates(
            db, project.id, episode_id, board.id, shot.id
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
                selected_image_version_id=selected_version_id,
                video_candidates=videos,
                video_candidates_truncated=videos_truncated,
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
