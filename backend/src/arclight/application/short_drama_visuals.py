"""Produce and select each exact shot using the durable media and Reviewer queues."""

from math import ceil
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContent, ShotContentV3, read_shot_content
from arclight.application import asset_objects
from arclight.application.audit import audit
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.continuity_completion import (
    complete_selected_review,
    complete_selected_video_review,
)
from arclight.application.continuity_selection import require_shot_review
from arclight.application.continuity_workflow import read_work, start_candidate_review
from arclight.application.dependencies import exact_version, require_manifest
from arclight.application.generation_jobs import create_job
from arclight.application.media_services import MediaSelection, account_selection
from arclight.application.production_versions import bind, find_binding
from arclight.application.render_manifests import output_profile
from arclight.application.short_drama_budget import root_for_job, root_for_workflow
from arclight.application.shot_production import generation_request
from arclight.application.timelines import approved_board, selected_clip_facts, verify_visual_object
from arclight.application.video_review import video_review_facts
from arclight.application.video_review_work import read_temporal_work
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import next_status
from arclight.domain.continuity_workflow import CandidateReviewCreate
from arclight.domain.dependencies import DependencyInput
from arclight.domain.generation import ACTIVE_STATUSES
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec, ShotReviewBinding
from arclight.domain.render_manifests import RenderOutputProfile
from arclight.domain.timelines import ShotSelection, VideoTrim
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation import GenerationAttempt
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    GenerationCandidate,
    GenerationJob,
    Shot,
    StoredFile,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.media_capabilities import resolve_capabilities
from arclight.providers.native_media_protocols import fitting_video_spec, validate_spec

SHOT_SELECTION = "SHORT_DRAMA_SHOT_SELECTION"
VISUAL_RIGHTS = "SHORT_DRAMA_VISUAL_RIGHTS"
MAX_GENERATIONS = 2
RETRYABLE_REVIEW_CODES = {
    "CONTINUITY_REVISION_REQUIRED",
    "VIDEO_REVISION_REQUIRED",
    "SHORT_DRAMA_VIDEO_TOO_SHORT",
}


def visual_authorized(
    db: Session,
    project_id: UUID,
    version: AssetVersion,
    file: StoredFile,
    short_drama_run_id: UUID,
) -> bool:
    """Keep the owner's exact production-use declaration alongside the original file record."""
    binding = find_binding(db, project_id, VISUAL_RIGHTS, version.id)
    if binding is None:
        return False
    evidence = binding.snapshot_json
    if evidence["run_id"] != str(short_drama_run_id):
        return False
    run = db.get(WorkflowRun, short_drama_run_id)
    candidate = db.get(GenerationCandidate, UUID(evidence["candidate_id"]))
    job = db.get(GenerationJob, candidate.job_id) if candidate else None
    return bool(
        run
        and candidate
        and candidate.source == "GENERATED"
        and candidate.asset_version_id == version.id
        and job
        and job.episode_id == run.episode_id
        and job.target_asset_id is None
        and root_for_job(db, job) == run
        and run.input_json["short_drama"].get("generated_media_rights_confirmed") is True
        and evidence["grant_sha256"] == digest(run.input_json["short_drama"])
        and evidence["actor"] == run.input_json["created_by"]
        and evidence["file_id"] == str(file.id)
        and file.asset_version_id == version.id
        and file.project_id == project_id
        and evidence["checksum"] == file.checksum == version.checksum
        and file.object_key == version.object_key
    )


def production_first_frame(
    db: Session, project_id: UUID, version: AssetVersion, run_id: UUID, target: dict
) -> bool:
    receipt = find_binding(db, project_id, VISUAL_RIGHTS, version.id)
    if receipt is None:
        return False
    evidence = receipt.snapshot_json
    if (
        evidence.get("purpose") != "VIDEO_FIRST_FRAME"
        or evidence.get("target") != target
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or version.stale
    ):
        return False
    candidate = db.get(GenerationCandidate, UUID(evidence["candidate_id"]))
    job = db.get(GenerationJob, candidate.job_id) if candidate else None
    if job is None or job.target_type != "SHOT_IMAGE":
        return False
    manifest = require_manifest(db, project_id, job.input_manifest_id)
    if manifest.snapshot_json["context"].get("continuity_review") != target:
        return False
    file = db.get(StoredFile, UUID(evidence["file_id"]))
    return bool(file and visual_authorized(db, project_id, version, file, run_id))


def bind_visual_rights(
    db: Session,
    run: WorkflowRun,
    shot: Shot,
    candidate: GenerationCandidate,
    version: AssetVersion,
    file: StoredFile,
    *,
    purpose: str,
) -> None:
    if file.project_id != candidate.project_id or file.checksum != version.checksum:
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "候选文件与精确媒体版本不一致", 409)
    bind(
        db,
        candidate.project_id,
        VISUAL_RIGHTS,
        version.id,
        {
            "run_id": str(run.id),
            "actor": run.input_json["created_by"],
            "grant_sha256": digest(run.input_json["short_drama"]),
            "candidate_id": str(candidate.id),
            "file_id": str(file.id),
            "checksum": version.checksum,
            "purpose": purpose,
            "target": {"storyboard_id": str(shot.storyboard_id), "shot_id": str(shot.id)},
        },
    )


def service_for(db: Session, actor: str, task: str) -> MediaSelection:
    service = account_selection(db, actor, task)
    if service is None:
        raise DomainError(
            "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED", "请配置短剧所需的图像和视频服务", 409
        )
    return service


def require_motion_service(db: Session, actor: str, shots: list[Shot], portrait: bool) -> None:
    service = service_for(db, actor, "VIDEO")
    config = service.configuration
    if resolve_capabilities(config["provider"], config["model"]).max_reference_images < 1:
        raise DomainError(
            "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED", "当前视频服务不支持冻结镜头图作为首帧", 409
        )
    probe = GenerationSpec(
        provider=config["provider"],
        model=config["model"],
        model_config_version=config["configuration_version"],
        prompt="镜头制作能力检查",
        prompt_version="short-drama-shots/1",
        output_spec={},
    )
    fitting_video_spec(
        probe,
        min_seconds=ceil(max(shot.duration_ms for shot in shots) / 1000),
        portrait=portrait,
    )


def visual_request(
    db: Session,
    project_id: UUID,
    board: Storyboard,
    shot: Shot,
    content: ShotContent,
    geometry: RenderOutputProfile,
    service: MediaSelection,
    first_frame: UUID | None,
) -> GenerationCreate:
    image = generation_request(db, project_id, board, shot)
    if image.target_asset_id is not None:
        raise DomainError(
            "SHORT_DRAMA_SHOT_PRESELECTED", "自动制作要求使用尚未选用素材的精确分镜", 409
        )
    config = service.configuration
    prompt = "\n".join(
        [
            image.spec.prompt,
            "人物：" + "、".join(content.characters),
            *[
                "；".join(
                    part
                    for part in (item.character, item.action, item.expression, item.gaze)
                    if part
                )
                for item in content.character_details
            ],
            content.notes,
        ]
    )[:10000]
    spec = GenerationSpec(
        provider=config["provider"],
        model=config["model"],
        model_config_version=config["configuration_version"],
        prompt=prompt,
        prompt_version="short-drama-shots/1",
        output_spec={"width": geometry.width, "height": geometry.height, "format": "png"},
    )
    capabilities = resolve_capabilities(spec.provider, spec.model)
    if first_frame is None:
        # Keep the frame's aspect ratio compatible with the video and retain the
        # exact storyboard dependency when an adapter cannot accept reference assets.
        width, height = (720, 1280) if geometry.height > geometry.width else (1280, 720)
        spec = spec.model_copy(
            update={"output_spec": {"width": width, "height": height, "format": "png"}}
        )
        references = image.inputs[1:]
        for source in references:
            version, asset = exact_version(db, project_id, source.source_asset_version_id)
            notes = version.metadata_json.get("asset_snapshot", {})
            value = notes.get("description") if isinstance(notes, dict) else None
            prompt += f"\n美术设计：{asset.name}" + (
                f"。{value[:300]}" if isinstance(value, str) else ""
            )
        spec = GenerationSpec.model_validate(
            spec.model_dump(mode="json") | {"prompt": prompt[:18000]}
        )
        validate_spec(spec, min(len(references), capabilities.max_reference_images))
        return image.model_copy(
            update={
                "spec": spec,
                "inputs": [image.inputs[0], *references[: capabilities.max_reference_images]],
            }
        )
    if capabilities.max_reference_images < 1:
        raise DomainError(
            "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED", "当前视频服务不支持冻结镜头图作为首帧", 409
        )
    spec = fitting_video_spec(
        spec,
        min_seconds=ceil(shot.duration_ms / 1000),
        portrait=geometry.height > geometry.width,
    )
    return GenerationCreate(
        episode_id=board.episode_id,
        target_type="VIDEO",
        task_type="VIDEO",
        scheduling_mode="BATCH",
        review_target=ShotReviewBinding(storyboard_id=board.id, shot_id=shot.id),
        inputs=[
            image.inputs[0],
            DependencyInput(
                source_asset_version_id=first_frame, dependency_type="REFERENCE_DEPENDENCY"
            ),
        ],
        spec=spec,
    )


def newest_candidate(db: Session, job: GenerationJob) -> GenerationCandidate | None:
    return db.scalar(
        select(GenerationCandidate)
        .where(GenerationCandidate.job_id == job.id, GenerationCandidate.source == "GENERATED")
        .order_by(GenerationCandidate.created_at.desc(), GenerationCandidate.id)
        .limit(1)
    )


def reviewed_version(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    job: GenerationJob,
    shot: Shot,
    candidate: GenerationCandidate,
) -> tuple[AssetVersion, UUID] | None:
    video = job.task_type == "VIDEO"
    project_id = job.project_id
    actor = run.input_json["created_by"]
    work = (
        read_temporal_work(db, project_id, candidate.id)
        if video
        else read_work(db, project_id, candidate.id)
    )
    if work is None:
        control = db.get(AgentRunLimit, run.id)
        if control is None:
            raise DomainError("SHORT_DRAMA_REVIEW_SCOPE", "缺少本次制作的执行范围", 409)
        start_candidate_review(
            db,
            settings,
            project_id,
            candidate.id,
            actor,
            CandidateReviewCreate(
                storyboard_id=shot.storyboard_id,
                shot_id=shot.id,
            ),
            producer_parent_run_id=run.id,
        )
        return None
    review = db.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == work.run_id)
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    )
    if review is None:
        return None
    if root_for_workflow(db, review.id) != run:
        raise DomainError("SHORT_DRAMA_REVIEW_SCOPE", "镜头审查不属于本次制作授权", 409)
    progress = review.pending_review or {}
    if progress.get("question_id") or progress.get("conclusion") == "HUMAN_REVIEW":
        raise DomainError(
            "SHORT_DRAMA_VISUAL_HUMAN_REQUIRED", f"镜头 {shot.shot_no} 需要人工核验", 409
        )
    if review.status in {"QUEUED", "RUNNING"}:
        return None
    if review.status not in {"WAITING_REVIEW", "SUCCEEDED"}:
        raise DomainError(
            review.error_code or "SHORT_DRAMA_VISUAL_REVIEW_FAILED",
            f"镜头 {shot.shot_no} 的质量审查已停止",
            409,
        )
    version = db.get(AssetVersion, candidate.asset_version_id, populate_existing=True)
    if version is None:
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "候选缺少精确媒体版本", 409)
    if video:
        facts = video_review_facts(db, version)
        if facts.record.report.conclusion != "PASS" or facts.decision is not None:
            raise DomainError("SHORT_DRAMA_VISUAL_HUMAN_REQUIRED", "视频需要人工核验", 409)
        if facts.media.duration_ms < shot.duration_ms:
            raise DomainError("SHORT_DRAMA_VIDEO_TOO_SHORT", "视频不足分镜计划时长", 409)
        report_id = facts.record.report.report_id
    else:
        record = require_shot_review(db, version)
        if record.review.conclusion != "PASS":
            raise DomainError("SHORT_DRAMA_VISUAL_HUMAN_REQUIRED", "镜头图需要人工核验", 409)
        report_id = record.review.result_id
    if (work.target.storyboard_id, work.target.shot_id, work.target.asset_version_id) != (
        shot.storyboard_id,
        shot.id,
        version.id,
    ):
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "候选审查与本次分镜镜头不匹配", 409)
    return version, report_id


def commit_visual(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    episode: Episode,
    shot: Shot,
    job: GenerationJob,
    candidate: GenerationCandidate,
    version: AssetVersion,
    report_id: UUID,
) -> ShotSelection:
    actor = run.input_json["created_by"]
    video = job.task_type == "VIDEO"
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True)
    if (
        asset is None
        or asset.project_id != episode.project_id
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or asset.current_version_id not in (None, version.id)
        or version.stale
        or job.target_asset_id is not None
    ):
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "候选当前状态无法安全选用", 409)
    file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if file is None:
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "候选文件与精确媒体版本不一致", 409)
    bind_visual_rights(db, run, shot, candidate, version, file, purpose="FINAL_SHOT")
    if version.status == "DRAFT":
        version.status = next_status(
            version.status, "READY_FOR_REVIEW", version.archived_from_status
        )
        version.revision += 1
    if version.status == "READY_FOR_REVIEW":
        version.status = next_status(version.status, "APPROVED", version.archived_from_status)
        version.revision += 1
    if asset.current_version_id is None:
        asset.current_version_id = version.id
        asset.revision += 1
        asset.lock_version += 1
    db.flush()
    selection = ShotSelection(
        shot_id=shot.id,
        asset_version_id=version.id,
        video=VideoTrim(duration_ms=shot.duration_ms) if video else None,
    )
    verify_visual_object(
        db,
        settings,
        episode,
        selected_clip_facts(
            db, episode, shot, version.id, 0, shot.duration_ms, trim=selection.video
        ),
        actor,
    )
    key = uuid5(run.id, f"visual:{shot.id}:{job.task_type}")
    bind(
        db,
        episode.project_id,
        SHOT_SELECTION,
        key,
        {
            "run_id": str(run.id),
            "storyboard_id": str(shot.storyboard_id),
            "selection": selection.model_dump(mode="json"),
            "job_id": str(job.id),
            "candidate_id": str(candidate.id),
            "review_id": str(report_id),
            "authorization_sha256": digest(run.input_json["short_drama"]),
        },
    )
    audit(
        db,
        episode.project_id,
        actor,
        "short_drama.visual_selected",
        version.id,
        {"run_id": str(run.id), "shot_id": str(shot.id), "review_id": str(report_id)},
    )
    if video:
        complete_selected_video_review(
            db, video_review_facts(db, version).record, short_drama_run_id=run.id
        )
    else:
        complete_selected_review(db, require_shot_review(db, version), short_drama_run_id=run.id)
    return selection


def advance_shot(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    episode: Episode,
    board: Storyboard,
    shot: Shot,
    content: ShotContent,
    geometry: RenderOutputProfile,
    *,
    first_frame: UUID | None = None,
    prepare_frame: bool = False,
) -> ShotSelection | None:
    task = "VIDEO" if first_frame is not None else "IMAGE"
    identity = uuid5(run.id, f"visual:{shot.id}:{task}")
    if receipt := find_binding(db, episode.project_id, SHOT_SELECTION, identity):
        record = receipt.snapshot_json
        selection = ShotSelection.model_validate(record["selection"])
        if (
            record["run_id"] != str(run.id)
            or record["storyboard_id"] != str(board.id)
            or selection.shot_id != shot.id
            or record["authorization_sha256"] != digest(run.input_json["short_drama"])
        ):
            raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "镜头选用记录不属于本次短剧制作", 409)
        if not prepare_frame:
            selected_clip_facts(
                db,
                episode,
                shot,
                selection.asset_version_id,
                0,
                shot.duration_ms,
                trim=selection.video,
            )
        else:
            frame, _ = exact_version(db, episode.project_id, selection.asset_version_id)
            if not production_first_frame(
                db,
                episode.project_id,
                frame,
                run.id,
                {"storyboard_id": str(board.id), "shot_id": str(shot.id)},
            ):
                raise DomainError("SHORT_DRAMA_FRAME_INVALID", "镜头首帧授权已失效", 409)
        return selection
    for index in range(MAX_GENERATIONS):
        key = str(uuid5(identity, f"generation:{index}"))
        job = db.scalar(
            select(GenerationJob).where(
                GenerationJob.project_id == episode.project_id, GenerationJob.idempotency_key == key
            )
        )
        if job is None:
            actor = run.input_json["created_by"]
            service = service_for(db, actor, task)
            payload = visual_request(
                db, episode.project_id, board, shot, content, geometry, service, first_frame
            )
            create_job(
                db,
                episode.project_id,
                actor,
                key,
                payload,
                frozen_service=service.private_snapshot(),
                short_drama_run_id=run.id,
                execution_policy=settings.media_execution_policy,
            )
            return None
        if root_for_job(db, job) != run or job.task_type != task:
            raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "媒体任务不属于本次镜头生产", 409)
        if job.status == "MANUAL_REVIEW":
            raise DomainError("SHORT_DRAMA_VISUAL_RESULT_UNKNOWN", "媒体结果需先对账", 409)
        if job.status in ACTIVE_STATUSES:
            return None
        if job.status == "CANCELLED":
            raise DomainError("SHORT_DRAMA_VISUAL_CANCELLED", "镜头媒体任务已取消", 409)
        if job.status == "FAILED":
            latest = db.scalar(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == job.id)
                .order_by(GenerationAttempt.attempt_no.desc())
                .limit(1)
            )
            error_code = latest.error_code if latest else None
            if error_code == "ACCOUNT_BALANCE_INSUFFICIENT":
                raise DomainError(error_code, "账户余额不足", 402)
            raise DomainError(
                error_code or "SHORT_DRAMA_VISUAL_FAILED", "镜头媒体任务未能生成可交付结果", 409
            )
        candidate = newest_candidate(db, job)
        if candidate is None:
            return None
        if candidate.asset_version_id is None:
            continue
        if prepare_frame:
            check = require_candidate_media_check(db, episode.project_id, candidate.id)
            version = db.get(AssetVersion, candidate.asset_version_id, populate_existing=True)
            file = db.scalar(
                select(StoredFile).where(StoredFile.asset_version_id == candidate.asset_version_id)
            )
            if version is None or file is None or check.image is None or version.stale:
                raise DomainError("SHORT_DRAMA_FRAME_INVALID", "镜头首帧未通过精确媒体检查", 409)
            bind_visual_rights(db, run, shot, candidate, version, file, purpose="VIDEO_FIRST_FRAME")
            asset_objects.read_object(
                settings, episode.project_id, version.id, version.object_key, version.checksum
            )
            selection = ShotSelection(shot_id=shot.id, asset_version_id=version.id)
            bind(
                db,
                episode.project_id,
                SHOT_SELECTION,
                identity,
                {
                    "run_id": str(run.id),
                    "storyboard_id": str(board.id),
                    "selection": selection.model_dump(mode="json"),
                    "job_id": str(job.id),
                    "candidate_id": str(candidate.id),
                    "authorization_sha256": digest(run.input_json["short_drama"]),
                },
            )
            audit(
                db,
                episode.project_id,
                run.input_json["created_by"],
                "short_drama.first_frame_prepared",
                version.id,
                {"run_id": str(run.id), "shot_id": str(shot.id)},
            )
            return selection
        try:
            result = reviewed_version(db, settings, run, job, shot, candidate)
        except DomainError as error:
            if error.code in RETRYABLE_REVIEW_CODES:
                continue
            raise
        if result is None:
            return None
        version, report_id = result
        return commit_visual(db, settings, run, episode, shot, job, candidate, version, report_id)
    raise DomainError(
        "SHORT_DRAMA_VISUAL_RETRY_EXHAUSTED",
        f"镜头 {shot.shot_no} 的素材在允许次数内未通过生产与审查",
        409,
    )


def advance_short_drama_visuals(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    storyboard_id: UUID,
) -> list[ShotSelection] | None:
    from arclight.application.short_drama_delivery import require_run_authorization

    episode, actor, grant = require_run_authorization(db, run)
    if run.status != "RUNNING" or grant.get("generated_media_rights_confirmed") is not True:
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "缺少本次短剧媒体制作授权", 409)
    board, shots = approved_board(db, episode, storyboard_id)
    if str(board.script_version_id) != grant["script_version_id"]:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "分镜须来自本次确认的精确剧本", 409)
    if not shots:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "自动制作需要有镜头的分镜", 422)
    geometry = output_profile(grant["output_profile"], "FORMAL")
    contents = [read_shot_content(shot.content_json) for shot in shots]
    if any(
        isinstance(content, ShotContentV3)
        and content.performance.motion_requirement == "dynamic_required"
        for content in contents
    ):
        raise DomainError(
            "SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED",
            "此表演分镜要求动态或口型核验，超出当前自动制作可审查范围",
            409,
        )
    motion = [
        shot
        for shot, content in zip(shots, contents, strict=True)
        if not isinstance(content, ShotContentV3)
    ]
    if motion:
        require_motion_service(db, actor, motion, geometry.height > geometry.width)
    selected = []
    for shot, content in zip(shots, contents, strict=True):
        selection = advance_shot(
            db,
            settings,
            run,
            episode,
            board,
            shot,
            content,
            geometry,
            prepare_frame=not isinstance(content, ShotContentV3),
        )
        if selection is None:
            continue
        if not isinstance(content, ShotContentV3):
            selection = advance_shot(
                db,
                settings,
                run,
                episode,
                board,
                shot,
                content,
                geometry,
                first_frame=selection.asset_version_id,
            )
        if selection is not None:
            selected.append(selection)
    return selected if len(selected) == len(shots) else None
