"""Produce and select each exact shot using the durable media and Reviewer queues."""

from math import ceil
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContent, read_shot_content
from arclight.application import asset_objects
from arclight.application.audit import audit
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.continuity_completion import (
    complete_selected_review,
    complete_selected_video_review,
)
from arclight.application.continuity_selection import require_shot_review
from arclight.application.continuity_workflow import (
    FrozenReviewTextService,
    read_work,
    start_candidate_review,
)
from arclight.application.dependencies import exact_version, require_manifest
from arclight.application.generation_jobs import create_job
from arclight.application.generation_scheduling import latest_media_receipt
from arclight.application.media_services import (
    MediaSelection,
    account_selection,
    credential_for_selection,
    restore_selection,
    selection_for_job,
)
from arclight.application.model_services import require_text_snapshot, run_settings, text_snapshot
from arclight.application.production_versions import bind, find_binding
from arclight.application.render_manifests import output_profile
from arclight.application.short_drama_budget import MEDIA_SCOPE, root_for_job, root_for_workflow
from arclight.application.short_drama_diagnostics import (
    ShortDramaFailure,
    media_failure_code,
    text_failure,
)
from arclight.application.shot_production import generation_request
from arclight.application.timelines import approved_board, selected_clip_facts, verify_visual_object
from arclight.application.video_review import video_review_facts
from arclight.application.video_review_work import read_temporal_work
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.asset_versions import next_status
from arclight.domain.continuity_workflow import CandidateReviewCreate
from arclight.domain.dependencies import DependencyInput
from arclight.domain.generation import ACTIVE_STATUSES, UNCERTAIN_STATUSES
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec, ShotReviewBinding
from arclight.domain.render_manifests import OutputGeometry, RenderOutputProfile
from arclight.domain.short_drama import ShortDramaTask
from arclight.domain.timelines import ShotSelection, VideoTrim
from arclight.domain.versions import SHORT_DRAMA_VISUAL_SERVICE, digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.generation import GenerationAttempt
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationCandidate,
    GenerationJob,
    Shot,
    StoredFile,
    Storyboard,
    VersionBinding,
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
RECONCILIATION_REQUIRED = "GATEWAY_RECONCILIATION_REQUIRED"


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


def service_failure(
    error: DomainError,
    service_kind: str,
    actor: str,
    service: MediaSelection | None,
    *,
    task: ShortDramaTask | None = None,
) -> ShortDramaFailure:
    return ShortDramaFailure(
        error,
        service_kind=service_kind,
        service=service.configuration if service else None,
        service_owner=service.created_by if service else actor,
        task=task,
    )


def service_for(
    db: Session, settings: Settings, project_id: UUID, actor: str, task: str
) -> MediaSelection:
    service = None
    try:
        service = account_selection(db, actor, task)
        if service is None:
            kind = "图像" if task == "IMAGE" else "视频"
            raise DomainError(
                "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED",
                f"请在模型配置中保存{kind}服务和 API Key",
                409,
            )
        credential_for_selection(db, settings, project_id, service)
    except DomainError as error:
        raise service_failure(error, task, actor, service) from error
    return service


def _visual_service_id(run_id: UUID, storyboard_id: UUID, task: str) -> UUID:
    return uuid5(run_id, f"short-drama-visual-service:{storyboard_id}:{task}")


def _visual_service_scope(run: WorkflowRun, storyboard_id: UUID, task: str) -> dict:
    return {
        "run_id": str(run.id),
        "storyboard_id": str(storyboard_id),
        "authorization_sha256": digest(run.input_json["short_drama"]),
        "task": task,
    }


def read_visual_service(
    db: Session, run: WorkflowRun, project_id: UUID, storyboard_id: UUID, task: str
) -> dict | None:
    row = find_binding(
        db, project_id, SHORT_DRAMA_VISUAL_SERVICE, _visual_service_id(run.id, storyboard_id, task)
    )
    if row is None:
        return None
    snapshot = row.snapshot_json
    scope = _visual_service_scope(run, storyboard_id, task)
    if "service" not in snapshot or any(snapshot.get(key) != value for key, value in scope.items()):
        raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "镜头服务与本次制作授权或分镜不一致", 409)
    return snapshot


def save_visual_service(
    db: Session,
    run: WorkflowRun,
    project_id: UUID,
    storyboard_id: UUID,
    task: str,
    service: dict | None,
) -> None:
    bind(
        db,
        project_id,
        SHORT_DRAMA_VISUAL_SERVICE,
        _visual_service_id(run.id, storyboard_id, task),
        {**_visual_service_scope(run, storyboard_id, task), "service": service},
    )


def frozen_media_service(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    project_id: UUID,
    storyboard_id: UUID,
    task: str,
) -> MediaSelection:
    actor = run.input_json["created_by"]
    frozen = read_visual_service(db, run, project_id, storyboard_id, task)
    if frozen is None:
        selected = service_for(db, settings, project_id, actor, task)
        save_visual_service(db, run, project_id, storyboard_id, task, selected.private_snapshot())
        return selected
    service: MediaSelection | None = None
    try:
        service = restore_selection(frozen["service"])
        credential_for_selection(db, settings, project_id, service)
    except DomainError as error:
        raise service_failure(error, task, actor, service) from error
    return service


def image_output(geometry: OutputGeometry) -> dict:
    width, height = (720, 1280) if geometry.height > geometry.width else (1280, 720)
    return {"width": width, "height": height, "format": "png"}


def generation_spec(service: MediaSelection, prompt: str, output: dict) -> GenerationSpec:
    config = service.configuration
    return GenerationSpec(
        provider=config["provider"],
        model=config["model"],
        model_config_version=config["configuration_version"],
        prompt=prompt,
        prompt_version="short-drama-shots/1",
        output_spec=output,
    )


def require_image_service(
    db: Session, settings: Settings, project_id: UUID, actor: str, geometry: OutputGeometry
) -> MediaSelection:
    service = service_for(db, settings, project_id, actor, "IMAGE")
    try:
        validate_spec(generation_spec(service, "镜头制作能力检查", image_output(geometry)))
    except DomainError as error:
        raise service_failure(error, "IMAGE", actor, service) from error
    return service


def motion_spec(
    service: MediaSelection, prompt: str, shot: Shot, geometry: OutputGeometry
) -> GenerationSpec:
    spec = generation_spec(service, prompt, {})
    if resolve_capabilities(spec.provider, spec.model).max_reference_images < 1:
        raise DomainError(
            "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED", "当前视频服务不支持冻结镜头图作为首帧", 409
        )
    return fitting_video_spec(
        spec, min_seconds=ceil(shot.duration_ms / 1000), portrait=geometry.height > geometry.width
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
    if first_frame is None:
        # Keep the frame's aspect ratio compatible with the video and retain the
        # exact storyboard dependency when an adapter cannot accept reference assets.
        references = image.inputs[1:]
        for source in references:
            version, asset = exact_version(db, project_id, source.source_asset_version_id)
            notes = version.metadata_json.get("asset_snapshot", {})
            value = notes.get("description") if isinstance(notes, dict) else None
            prompt += f"\n美术设计：{asset.name}" + (
                f"。{value[:300]}" if isinstance(value, str) else ""
            )
        spec = generation_spec(service, prompt[:18000], image_output(geometry))
        capabilities = resolve_capabilities(spec.provider, spec.model)
        validate_spec(spec, min(len(references), capabilities.max_reference_images))
        return image.model_copy(
            update={
                "spec": spec,
                "inputs": [image.inputs[0], *references[: capabilities.max_reference_images]],
            }
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
        spec=motion_spec(service, prompt, shot, geometry),
    )


def authorized_visual_board(
    db: Session, run: WorkflowRun, storyboard_id: UUID
) -> tuple[Episode, str, Storyboard, list[Shot], RenderOutputProfile]:
    from arclight.application.short_drama_delivery import require_run_authorization

    episode, actor, grant = require_run_authorization(db, run)
    if run.status != "RUNNING" or grant.get("generated_media_rights_confirmed") is not True:
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "缺少本次短剧媒体制作授权", 409)
    board, shots = approved_board(db, episode, storyboard_id)
    if str(board.script_version_id) != grant["script_version_id"]:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "分镜须来自本次确认的精确剧本", 409)
    if not shots:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "自动制作需要有镜头的分镜", 422)
    return episode, actor, board, shots, output_profile(grant["output_profile"], "FORMAL")


def shot_production_plan(shots: list[Shot]) -> list[tuple[Shot, ShotContent, bool]]:
    plan = []
    for shot in shots:
        content = read_shot_content(shot.content_json)
        performance = getattr(content, "performance", None)
        if performance is not None and performance.motion_requirement == "dynamic_required":
            raise DomainError(
                "SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED",
                f"镜头 {shot.shot_no} 要求动态或口型核验，需要人工确认制作方式",
                409,
            )
        # Retained boards without performance directions use the first-frame video route.
        plan.append((shot, content, performance is None))
    return plan


def require_review_service(
    db: Session, settings: Settings, actor: str, min_images: int, *, tools: bool
) -> dict | None:
    try:
        service = require_text_snapshot(db, actor, settings)
    except DomainError as error:
        raise text_failure(error, actor, text_snapshot(db, actor), review=True) from error
    if service is None:
        capabilities = settings.llm_agent_capabilities
        response_format = settings.llm_response_format
    else:
        facts = service.get("agent_capabilities")
        capabilities = AgentModelCapabilities.model_validate(facts) if facts else None
        response_format = service["response_format"]
    if (
        capabilities is None
        or response_format not in capabilities.response_formats
        or capabilities.max_input_images < min_images
        or (tools and not capabilities.tools)
    ):
        feature = "、工具调用" if tools else ""
        unsupported = DomainError(
            "SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED",
            f"镜头审查要求文字模型支持结构化输出{feature}和至少 {min_images} 张输入图片",
            422,
        )
        raise text_failure(unsupported, actor, service, review=True)
    return service


def frozen_review_service(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    project_id: UUID,
    storyboard_id: UUID,
    plan: list[tuple[Shot, ShotContent, bool]] | None = None,
) -> dict | None:
    actor = run.input_json["created_by"]
    frozen = read_visual_service(db, run, project_id, storyboard_id, "REVIEW")
    if frozen is not None:
        service = frozen["service"]
        try:
            run_settings(settings, service, db)
        except DomainError as error:
            raise text_failure(error, actor, service, review=True) from error
        return service
    if plan is None:
        # A review can be admitted after its media job has already frozen its own route.
        _, _, _, shots, _ = authorized_visual_board(db, run, storyboard_id)
        plan = shot_production_plan(shots)
    service = require_review_service(
        db,
        settings,
        actor,
        2 if any(needs_motion for _, _, needs_motion in plan) else 1,
        tools=any(not needs_motion for _, _, needs_motion in plan),
    )
    save_visual_service(db, run, project_id, storyboard_id, "REVIEW", service)
    return service


def preflight_visuals(
    db: Session, settings: Settings, run: WorkflowRun, storyboard_id: UUID
) -> None:
    """Check the exact approved shot sources and configured routes before paid media work."""
    episode, actor, board, shots, geometry = authorized_visual_board(db, run, storyboard_id)
    plan = shot_production_plan(shots)
    image = frozen_media_service(db, settings, run, episode.project_id, board.id, "IMAGE")
    motion_shots = []
    for shot, content, needs_motion in plan:
        try:
            visual_request(db, episode.project_id, board, shot, content, geometry, image, None)
        except DomainError as error:
            raise service_failure(error, "IMAGE", actor, image) from error
        if needs_motion:
            motion_shots.append(shot)
    if motion_shots:
        video = frozen_media_service(db, settings, run, episode.project_id, board.id, "VIDEO")
        try:
            for shot in motion_shots:
                motion_spec(video, "镜头制作能力检查", shot, geometry)
        except DomainError as error:
            raise service_failure(error, "VIDEO", actor, video) from error
    # Temporal review submits one contact sheet and the exact first frame.
    frozen_review_service(db, settings, run, episode.project_id, board.id, plan)


def visual_job_failure(db: Session, job: GenerationJob, error: DomainError) -> ShortDramaFailure:
    try:
        service = selection_for_job(db, job)
    except DomainError:
        service = None
    return service_failure(
        error,
        job.task_type,
        job.created_by,
        service,
        task=ShortDramaTask(id=job.id, family="GENERATION"),
    )


def failed_media(db: Session, job: GenerationJob) -> ShortDramaFailure:
    receipt = latest_media_receipt(db, job)
    receipt_code = receipt.error_code if receipt else None
    if receipt_code == "PROVIDER_INSUFFICIENT_BALANCE":
        error = DomainError("ACCOUNT_BALANCE_INSUFFICIENT", "账户余额不足", 402)
    else:
        operation_code = db.scalar(
            select(GenerationAttempt.error_code).where(
                GenerationAttempt.job_id == job.id,
                GenerationAttempt.attempt_no == job.attempt_count,
            )
        )
        code = media_failure_code(receipt_code, operation_code)
        if not code or code == "ACCOUNT_BALANCE_INSUFFICIENT":
            code = "SHORT_DRAMA_VISUAL_FAILED"
        error = DomainError(code, "镜头媒体任务未能生成可交付结果", 409)
    return visual_job_failure(db, job, error)


def review_reconciling(db: Session, review: WorkflowRun) -> bool:
    return (
        db.scalar(
            select(AgentLoop.id)
            .where(
                AgentLoop.run_id == review.id,
                AgentLoop.status == "RECOVERY_REQUIRED",
                AgentLoop.outcome_json["stop_code"].as_string() == RECONCILIATION_REQUIRED,
            )
            .limit(1)
        )
        is not None
    )


def visual_reconciliation(
    db: Session, run: WorkflowRun, storyboard_id: UUID
) -> ShortDramaFailure | None:
    job = db.scalar(
        select(GenerationJob)
        .join(VersionBinding, VersionBinding.resource_id == GenerationJob.id)
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .where(
            VersionBinding.project_id == GenerationJob.project_id,
            VersionBinding.resource_type == MEDIA_SCOPE,
            VersionBinding.snapshot_json["run_id"].as_string() == str(run.id),
            GenerationJob.episode_id == run.episode_id,
            GenerationJob.task_type.in_({"IMAGE", "VIDEO"}),
            GenerationJob.status.in_(UNCERTAIN_STATUSES),
            DependencyManifest.snapshot_json["context"]["continuity_review"][
                "storyboard_id"
            ].as_string()
            == str(storyboard_id),
        )
        .order_by(GenerationJob.created_at, GenerationJob.id)
        .limit(1)
    )
    if job is not None:
        return visual_job_failure(
            db,
            job,
            DomainError("SHORT_DRAMA_VISUAL_RESULT_UNKNOWN", "请先对账原媒体订单", 409),
        )
    review = db.scalar(
        select(WorkflowRun)
        .join(AgentRunLimit, AgentRunLimit.run_id == WorkflowRun.id)
        .join(AgentLoop, AgentLoop.run_id == WorkflowRun.id)
        .where(
            AgentRunLimit.parent_run_id == run.id,
            AgentRunLimit.root_run_id == run.id,
            WorkflowRun.episode_id == run.episode_id,
            WorkflowRun.workflow_type == "continuity_review",
            WorkflowRun.status.in_({"QUEUED", "RUNNING", "WAITING_REVIEW"}),
            AgentLoop.status == "RECOVERY_REQUIRED",
            AgentLoop.outcome_json["stop_code"].as_string() == RECONCILIATION_REQUIRED,
            AgentLoop.seed_json["messages"][0]["data"]["target"]["storyboard_id"].as_string()
            == str(storyboard_id),
        )
        .order_by(WorkflowRun.created_at, WorkflowRun.id)
        .limit(1)
    )
    if review is None:
        return None
    return text_failure(
        DomainError(RECONCILIATION_REQUIRED, "请先对账原审查模型调用", 409),
        review.input_json["created_by"],
        review.input_json.get("_text_service"),
        review=True,
        task=ShortDramaTask(id=review.id, family="WORKFLOW"),
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
        service = frozen_review_service(db, settings, run, project_id, shot.storyboard_id)
        try:
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
                frozen_text_service=FrozenReviewTextService(service),
            )
        except DomainError as error:
            raise text_failure(error, actor, service, review=True) from error
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
    owner = review.input_json["created_by"]
    service = review.input_json.get("_text_service")
    if review.status == "WAITING_REVIEW" and review_reconciling(db, review):
        return None
    progress = review.pending_review or {}
    if progress.get("question_id") or progress.get("conclusion") == "HUMAN_REVIEW":
        raise text_failure(
            DomainError(
                "SHORT_DRAMA_VISUAL_HUMAN_REQUIRED", f"镜头 {shot.shot_no} 需要人工核验", 409
            ),
            owner,
            service,
            review=True,
        )
    if review.status in {"QUEUED", "RUNNING"}:
        return None
    if review.status not in {"WAITING_REVIEW", "SUCCEEDED"}:
        code = review.error_code or "SHORT_DRAMA_VISUAL_REVIEW_FAILED"
        message = (
            "请先对账原审查模型调用"
            if code == RECONCILIATION_REQUIRED
            else f"镜头 {shot.shot_no} 的质量审查已停止"
        )
        raise text_failure(
            DomainError(code, message, 402 if code == "ACCOUNT_BALANCE_INSUFFICIENT" else 409),
            owner,
            service,
            review=True,
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
            service = frozen_media_service(db, settings, run, episode.project_id, board.id, task)
            try:
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
            except DomainError as error:
                raise service_failure(error, task, actor, service) from error
            return None
        if root_for_job(db, job) != run or job.task_type != task:
            raise DomainError("SHORT_DRAMA_VISUAL_SCOPE", "媒体任务不属于本次镜头生产", 409)
        if job.status in ACTIVE_STATUSES or job.status in UNCERTAIN_STATUSES:
            return None
        if job.status == "CANCELLED":
            raise visual_job_failure(
                db, job, DomainError("SHORT_DRAMA_VISUAL_CANCELLED", "镜头媒体任务已取消", 409)
            )
        if job.status == "FAILED":
            raise failed_media(db, job)
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
    episode, _, board, shots, geometry = authorized_visual_board(db, run, storyboard_id)
    selected = []
    for shot, content, needs_motion in shot_production_plan(shots):
        selection = advance_shot(
            db,
            settings,
            run,
            episode,
            board,
            shot,
            content,
            geometry,
            prepare_frame=needs_motion,
        )
        if selection is None:
            continue
        if needs_motion:
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
