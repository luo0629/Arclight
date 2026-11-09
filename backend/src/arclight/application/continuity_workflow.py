"""Bind checked candidates to the existing durable Reviewer and revision runtime."""

import logging
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from arclight.application.agent_runtime_state import initialize_run_state
from arclight.application.agent_tools import grant_agent_tools
from arclight.application.audit import audit
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.commands import emit, require_episode, require_project
from arclight.application.continuity_review_store import REVIEW_RESOURCE, require_review_record
from arclight.application.continuity_reviews import require_review_target
from arclight.application.continuity_revision_store import job_revision
from arclight.application.continuity_routing import read_routed_review
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import text_snapshot
from arclight.application.production_versions import bind, checked_snapshot, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.continuity_reviews import ReviewRerun, ReviewTarget
from arclight.domain.continuity_workflow import (
    REVIEW_BLOCK_RESOURCE,
    REVIEW_WORK_RESOURCE,
    REVIEW_WORKFLOW_VERSION,
    CandidateReviewCreate,
    CandidateReviewWork,
)
from arclight.domain.generation_requests import GenerationCreate, ShotReviewBinding
from arclight.domain.versions import digest
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationJob,
    OutboxEvent,
    Shot,
    Storyboard,
    VersionBinding,
    WorkflowRun,
)

logger = logging.getLogger(__name__)


def require_shot_binding(
    db: Session, project_id: UUID, binding: ShotReviewBinding, episode_id: UUID | None
) -> Storyboard:
    board = db.get(Storyboard, binding.storyboard_id, populate_existing=True)
    episode = db.get(Episode, board.episode_id, populate_existing=True) if board else None
    shot = db.get(Shot, binding.shot_id, populate_existing=True)
    if (
        board is None
        or episode is None
        or episode.project_id != project_id
        or episode.status == "ARCHIVED"
        or (episode_id is not None and episode.id != episode_id)
        or board.status != "APPROVED"
        or shot is None
        or shot.storyboard_id != board.id
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "送审须绑定本项目已批准的精确分镜和镜头", 409)
    return board


def freeze_review_binding(db: Session, project_id: UUID, payload: GenerationCreate) -> dict | None:
    legacy = {
        name: payload.spec.context[name]
        for name in ("storyboard_id", "shot_id")
        if name in payload.spec.context
    }
    if payload.review_target is None and len(legacy) < 2:
        # Partial legacy context is generic metadata, never an exact shot binding.
        return None
    if payload.target_type != "SHOT_IMAGE":
        raise DomainError("REVIEW_TARGET_INVALID", "连续性送审只适用于镜头图片", 422)
    try:
        binding = payload.review_target or ShotReviewBinding.model_validate(legacy)
        if legacy and ShotReviewBinding.model_validate(legacy) != binding:
            raise ValueError("Conflicting exact targets")
    except (ValidationError, ValueError) as error:
        raise DomainError("REVIEW_TARGET_INVALID", "分镜和镜头绑定必须完整且一致", 422) from error
    require_shot_binding(db, project_id, binding, payload.episode_id)
    if not any(
        item.source_asset_version_id == binding.storyboard_id
        and item.dependency_type == "CONTENT_DEPENDENCY"
        and (not item.selectors or "shot:" + str(binding.shot_id) in item.selectors)
        for item in payload.inputs
    ):
        raise DomainError("REVIEW_DEPENDENCY_REQUIRED", "送审镜头须作为精确分镜内容依赖冻结", 422)
    return binding.model_dump(mode="json")


def read_work(db: Session, project_id: UUID, candidate_id: UUID) -> CandidateReviewWork | None:
    row = find_binding(db, project_id, REVIEW_WORK_RESOURCE, candidate_id)
    if row is None:
        return None
    return CandidateReviewWork.model_validate(checked_snapshot(db, row)["continuity_work"])


def start_candidate_review(
    db: Session,
    settings: Settings,
    project_id: UUID,
    candidate_id: UUID,
    actor: str,
    payload: CandidateReviewCreate,
) -> WorkflowRun:
    lock_gate(db)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    candidate = db.get(GenerationCandidate, candidate_id, populate_existing=True)
    if candidate is None or candidate.project_id != project_id:
        raise DomainError("NOT_FOUND", "生成候选不存在", 404)
    checked = require_candidate_media_check(db, project_id, candidate.id)
    job = db.get(GenerationJob, candidate.job_id)
    assert job is not None
    manifest = db.get(DependencyManifest, candidate.input_manifest_id)
    assert manifest is not None
    context = manifest.snapshot_json["context"]
    frozen = context.get("continuity_review")
    legacy = context["generation"]["context"]
    if frozen is None and all(name in legacy for name in ("storyboard_id", "shot_id")):
        frozen = {name: legacy.get(name) for name in ("storyboard_id", "shot_id")}
    inherited = (
        read_work(db, project_id, candidate.replaces_candidate_id)
        if candidate.replaces_candidate_id
        else None
    )
    if inherited is not None:
        frozen = inherited.target.model_dump(mode="json", include={"storyboard_id", "shot_id"})
    if frozen is not None and ShotReviewBinding.model_validate(frozen) != ShotReviewBinding(
        storyboard_id=payload.storyboard_id, shot_id=payload.shot_id
    ):
        raise DomainError("REVIEW_TASK_CONFLICT", "送审不能更换生成或替换来源的精确镜头", 409)
    board = require_shot_binding(db, project_id, payload, job.episode_id)
    version = db.get(AssetVersion, candidate.asset_version_id)
    assert version is not None
    target = ReviewTarget(
        candidate_id=candidate.id,
        asset_id=version.asset_id,
        asset_version_id=version.id,
        input_manifest_id=candidate.input_manifest_id,
        media_check_sha256=digest(checked.model_dump(mode="json")),
        storyboard_id=board.id,
        shot_id=payload.shot_id,
    )
    require_review_target(db, project_id, board.episode_id, target, actor)
    previous = read_work(db, project_id, candidate.id)
    if previous is not None:
        if previous.target != target:
            raise DomainError("REVIEW_TASK_CONFLICT", "同一候选不能更换精确送审目标", 409)
        run = db.get(WorkflowRun, previous.run_id)
        assert run is not None
        if previous.revision_id is None and run.input_json["continuity_review"][
            "agent_limits"
        ] != payload.agent_limits.model_dump(mode="json"):
            raise DomainError("AGENT_LIMITS_CONFLICT", "送审不能重置原运行限制", 409)
        return run
    latest = db.scalar(
        select(VersionBinding)
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == REVIEW_RESOURCE,
            VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                "candidate_id"
            ].as_string()
            == str(candidate.id),
        )
        .order_by(VersionBinding.snapshot_json["review_contract"]["sequence"].as_integer().desc())
        .limit(1)
    )
    rerun = None
    if latest is not None:
        record = require_review_record(db, project_id, latest.resource_id, actor)
        if record.review.target != target:
            raise DomainError("REVIEW_TASK_CONFLICT", "已有审查不能更换精确镜头绑定", 409)
        rerun = ReviewRerun(
            previous_review_id=record.review.result_id,
            previous_sha256=digest(record.model_dump(mode="json")),
            reason="接入持久生产送审，复核该候选的精确历史审查",
        )
    revision = job_revision(db, job) if candidate.source == "GENERATED" else None
    if revision is not None:
        _, source_route = read_routed_review(db, revision.request.review_id)
        run = db.get(WorkflowRun, source_route.run_id, with_for_update=True)
        if run is None or run.workflow_type != "continuity_review":
            raise DomainError("REVIEW_RUN_REQUIRED", "修订须沿用原连续性审查运行", 409)
        if run.status not in {"QUEUED", "RUNNING", "WAITING_REVIEW"}:
            raise DomainError("REVIEW_RUN_INACTIVE", "原审查运行已停止，不能重置修订限制", 409)
        # Append work and its wake-up atomically, including a running executor
        # which may already have read its previous work page.
        run.status, run.pending_review = "QUEUED", None
        run.revision += 1
        db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    else:
        from arclight.application.continuity_reviewer import reviewer_registry
        from arclight.application.production_versions import capture_text

        episode = require_episode(db, board.episode_id, actor, write=True)
        run = WorkflowRun(
            id=uuid5(candidate.id, REVIEW_WORKFLOW_VERSION),
            episode_id=episode.id,
            workflow_type="continuity_review",
            workflow_version=REVIEW_WORKFLOW_VERSION,
            trace_id="continuity:" + str(candidate.id),
            idempotency_key="continuity:" + str(candidate.id),
            request_fingerprint=digest(target.model_dump(mode="json")),
            input_json={
                "created_by": actor,
                "script_version_id": str(board.script_version_id),
                "target_duration_ms": episode.target_duration_ms,
                "continuity_review": {
                    "target": target.model_dump(mode="json"),
                    "rerun": rerun.model_dump(mode="json") if rerun else None,
                    "agent_limits": payload.agent_limits.model_dump(mode="json"),
                },
            },
        )
        snapshot = text_snapshot(db, actor)
        if snapshot:
            run.input_json = run.input_json | {"_text_service": snapshot}
        db.add(run)
        db.flush()
        initialize_run_state(run, project_id)
        capture_text(db, project_id, run, settings, actor=actor)
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run.id,
                agent_name="continuity_reviewer",
                allowed_tools=["investigate_continuity", "check_continuity_rules"],
            ),
            actor,
            reviewer_registry(settings),
        )
        audit(
            db, project_id, actor, "workflow.started", run.id, {"candidate_id": str(candidate.id)}
        )
        db.add(OutboxEvent(run_id=run.id, revision=0))
        emit(db, run, "workflow.queued")
    work = CandidateReviewWork(
        run_id=run.id,
        candidate_id=candidate.id,
        revision_id=revision.revision_id if revision else None,
        target=target,
        rerun=rerun,
    )
    bind(
        db,
        project_id,
        REVIEW_WORK_RESOURCE,
        candidate.id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [],
            "parameters": {},
            "parameter_sha256": digest({}),
            "continuity_work": work.model_dump(mode="json"),
        },
    )
    emit(db, run, "continuity_review.queued", work.model_dump(mode="json"))
    return run


def queue_pending_reviews(
    factory: sessionmaker[Session], settings: Settings, limit: int = 100
) -> int:
    """Candidate rows are durable intake facts; a lost dispatch never loses a review."""
    current_work, source_work, blocked = (aliased(VersionBinding) for _ in range(3))
    with factory() as db:
        identities = list(
            db.scalars(
                select(GenerationCandidate.id)
                .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
                .join(
                    DependencyManifest,
                    DependencyManifest.id == GenerationCandidate.input_manifest_id,
                )
                .outerjoin(
                    current_work,
                    (current_work.resource_type == REVIEW_WORK_RESOURCE)
                    & (current_work.resource_id == GenerationCandidate.id),
                )
                .outerjoin(
                    source_work,
                    (source_work.resource_type == REVIEW_WORK_RESOURCE)
                    & (source_work.resource_id == GenerationCandidate.replaces_candidate_id),
                )
                .outerjoin(
                    blocked,
                    (blocked.resource_type == REVIEW_BLOCK_RESOURCE)
                    & (blocked.resource_id == GenerationCandidate.id),
                )
                .where(
                    blocked.id.is_(None),
                    GenerationJob.target_type == "SHOT_IMAGE",
                    GenerationCandidate.asset_version_id.is_not(None),
                    current_work.id.is_(None),
                    or_(
                        DependencyManifest.snapshot_json["context"]["continuity_review"]["shot_id"]
                        .as_string()
                        .is_not(None),
                        DependencyManifest.snapshot_json["context"]["continuity_revision"][
                            "target"
                        ]["shot_id"]
                        .as_string()
                        .is_not(None),
                        and_(
                            DependencyManifest.snapshot_json["context"]["generation"]["context"][
                                "storyboard_id"
                            ]
                            .as_string()
                            .is_not(None),
                            DependencyManifest.snapshot_json["context"]["generation"]["context"][
                                "shot_id"
                            ]
                            .as_string()
                            .is_not(None),
                        ),
                        source_work.id.is_not(None),
                    ),
                )
                .order_by(GenerationCandidate.created_at, GenerationCandidate.id)
                .limit(limit)
            )
        )
    queued = 0
    for identity in identities:
        try:
            with factory.begin() as db:
                lock_gate(db)
                candidate = db.get(GenerationCandidate, identity)
                assert candidate is not None
                project_id = candidate.project_id
                if read_work(db, candidate.project_id, candidate.id) is not None:
                    continue
                job = db.get(GenerationJob, candidate.job_id)
                manifest = db.get(DependencyManifest, candidate.input_manifest_id)
                assert job is not None and manifest is not None
                context = manifest.snapshot_json["context"]
                revision = job_revision(db, job)
                source = (
                    read_work(db, job.project_id, candidate.replaces_candidate_id)
                    if candidate.replaces_candidate_id
                    else None
                )
                target = (
                    revision.target.model_dump(mode="json")
                    if revision
                    else source.target.model_dump(mode="json")
                    if source
                    else context.get("continuity_review") or context["generation"]["context"]
                )
                payload = CandidateReviewCreate.model_validate(
                    {"storyboard_id": target["storyboard_id"], "shot_id": target["shot_id"]}
                )
                actor = job.created_by if candidate.source == "GENERATED" else candidate.created_by
                start_candidate_review(db, settings, job.project_id, candidate.id, actor, payload)
                queued += 1
        except (DomainError, ValidationError, KeyError) as error:
            code = error.code if isinstance(error, DomainError) else "REVIEW_TARGET_INVALID"
            if not isinstance(error, DomainError) or error.status_code < 500:
                with factory.begin() as db:
                    lock_gate(db)
                    if find_binding(db, project_id, REVIEW_BLOCK_RESOURCE, identity) is None:
                        bind(
                            db,
                            project_id,
                            REVIEW_BLOCK_RESOURCE,
                            identity,
                            {
                                "schema_version": 1,
                                "compatibility": "EXACT_DEFINITIONS",
                                "components": [],
                                "parameters": {},
                                "parameter_sha256": digest({}),
                                "blocked_review": {"error_code": code},
                            },
                        )
            # Invalid/stale scope remains unreviewed and unselectable. One bad job
            # cannot prevent healthy candidates in this page from being scheduled.
            logger.info(
                "review_intake_blocked candidate_id=%s code=%s",
                identity,
                error.code if isinstance(error, DomainError) else "REVIEW_TARGET_INVALID",
            )
    return queued
