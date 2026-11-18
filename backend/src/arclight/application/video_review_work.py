"""Intake and live identity checks for exact candidate video review work."""

from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import initialize_run_state
from arclight.application.audit import audit
from arclight.application.commands import emit, require_project
from arclight.application.continuity_revision_store import job_revision, require_revision_target
from arclight.application.continuity_workflow import require_shot_binding
from arclight.application.model_services import text_snapshot
from arclight.application.production_versions import (
    bind,
    capture_text,
    checked_snapshot,
    find_binding,
)
from arclight.application.video_candidates import require_video_candidate_media_check
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.candidate_checks import CandidateVideoCheck
from arclight.domain.continuity_workflow import CandidateReviewCreate
from arclight.domain.generation_requests import ShotReviewBinding
from arclight.domain.versions import digest
from arclight.domain.video_review import VideoReviewTarget
from arclight.domain.video_temporal_reviewer import (
    TEMPORAL_WORK_RESOURCE,
    TEMPORAL_WORKFLOW_VERSION,
    TemporalWork,
)
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationCandidate,
    GenerationJob,
    OutboxEvent,
    Shot,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)


@dataclass(frozen=True)
class VideoReviewInputs:
    target: VideoReviewTarget
    check: CandidateVideoCheck
    manifest: DependencyManifest
    version: AssetVersion
    job: GenerationJob
    shot: Shot


def review_inputs(db: Session, project_id: UUID, candidate_id: UUID) -> VideoReviewInputs:
    check = require_video_candidate_media_check(db, project_id, candidate_id)
    candidate = db.get(GenerationCandidate, candidate_id)
    assert candidate is not None
    manifest = db.get(DependencyManifest, candidate.input_manifest_id)
    job = db.get(GenerationJob, candidate.job_id)
    version = db.get(AssetVersion, candidate.asset_version_id, populate_existing=True)
    assert (
        manifest is not None and job is not None and version is not None and check.video is not None
    )
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True)
    assert asset is not None
    if (
        job.episode_id is None
        or version.stale
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
    ):
        raise DomainError("VIDEO_REVIEW_TARGET_INVALID", "镜头视频已失效或不可审查", 409)
    source = version.metadata_json["video_source"]
    binding = ShotReviewBinding(storyboard_id=source["storyboard_id"], shot_id=source["shot_id"])
    require_shot_binding(db, project_id, binding, job.episode_id)
    shot = db.get(Shot, binding.shot_id)
    assert shot is not None
    target = VideoReviewTarget(
        candidate_id=candidate.id,
        asset_id=asset.id,
        asset_version_id=version.id,
        input_manifest_id=manifest.id,
        media_check_sha256=digest(check.model_dump(mode="json")),
        storyboard_id=binding.storyboard_id,
        shot_id=binding.shot_id,
        checksum=version.checksum,
        manifest_sha256=digest(manifest.snapshot_json),
    )
    require_revision_target(db, job, target)
    return VideoReviewInputs(target, check, manifest, version, job, shot)


def read_temporal_work(db: Session, project_id: UUID, candidate_id: UUID) -> TemporalWork | None:
    row = find_binding(db, project_id, TEMPORAL_WORK_RESOURCE, candidate_id)
    if row is None:
        return None
    work = TemporalWork.model_validate(checked_snapshot(db, row)["temporal_work"])
    if work.target.candidate_id != candidate_id:
        raise DomainError("VIDEO_REVIEW_TARGET_INVALID", "送审记录与候选身份不一致", 409)
    return work


def start_temporal_review(
    db: Session,
    settings: Settings,
    project_id: UUID,
    candidate_id: UUID,
    actor: str,
    payload: CandidateReviewCreate,
    *,
    producer_parent_run_id: UUID | None = None,
) -> WorkflowRun:
    # Called by the common intake with the gate and dependency graph held.
    require_project(db, project_id, actor, write=True)
    inputs = review_inputs(db, project_id, candidate_id)
    target = inputs.target
    if (target.storyboard_id, target.shot_id) != (payload.storyboard_id, payload.shot_id):
        raise DomainError("REVIEW_TASK_CONFLICT", "送审不能更换视频的精确分镜或镜头", 409)
    if producer_parent_run_id is not None:
        from arclight.application.producer_delegation import require_producer_parent

        assert inputs.job.episode_id is not None
        require_producer_parent(db, producer_parent_run_id, inputs.job.episode_id, actor)
    parent = job_revision(db, inputs.job)
    previous = read_temporal_work(db, project_id, candidate_id)
    if previous is not None:
        run = db.get(WorkflowRun, previous.run_id)
        if run is None or previous.target != target:
            raise DomainError("REVIEW_TASK_CONFLICT", "同一候选不能更换送审目标", 409)
        if parent is None and run.input_json["video_temporal_review"][
            "agent_limits"
        ] != payload.agent_limits.model_dump(mode="json"):
            raise DomainError("AGENT_LIMITS_CONFLICT", "送审不能重置审查限制", 409)
        require_temporal_parent(db, run, producer_parent_run_id)
        return run

    episode = db.get(Episode, inputs.job.episode_id)
    assert episode is not None
    run = None
    if parent is not None:
        prior = read_temporal_work(db, project_id, parent.target.candidate_id)
        run = db.get(WorkflowRun, prior.run_id, with_for_update=True) if prior is not None else None
        if (
            run is None
            or run.workflow_version != TEMPORAL_WORKFLOW_VERSION
            or run.episode_id != episode.id
            or run.status not in {"QUEUED", "RUNNING", "WAITING_REVIEW"}
        ):
            raise DomainError("REVIEW_RUN_INACTIVE", "返修视频须沿用原时序审查运行", 409)
        require_temporal_parent(db, run, producer_parent_run_id)
        run.status, run.pending_review = "QUEUED", None
        run.revision += 1
    else:
        board = db.get(Storyboard, target.storyboard_id)
        assert board is not None
        run = WorkflowRun(
            id=uuid5(candidate_id, TEMPORAL_WORKFLOW_VERSION),
            episode_id=episode.id,
            workflow_type="continuity_review",
            workflow_version=TEMPORAL_WORKFLOW_VERSION,
            trace_id="video-review:" + str(candidate_id),
            idempotency_key="video-review:" + str(candidate_id),
            request_fingerprint=digest(target.model_dump(mode="json")),
            input_json={
                "created_by": actor,
                "script_version_id": str(board.script_version_id),
                "target_duration_ms": episode.target_duration_ms,
                "video_temporal_review": {
                    "agent_limits": payload.agent_limits.model_dump(mode="json")
                },
            },
        )
        snapshot = text_snapshot(db, actor)
        if snapshot:
            run.input_json = run.input_json | {"_text_service": snapshot}
        db.add(run)
        db.flush()
        initialize_run_state(run, project_id)
        if producer_parent_run_id is not None:
            from arclight.application.agent_limits import configure_agent_run

            configure_agent_run(
                db, run.id, actor, payload.agent_limits, parent_run_id=producer_parent_run_id
            )
        capture_text(db, project_id, run, settings, actor=actor)
        audit(
            db, project_id, actor, "workflow.started", run.id, {"candidate_id": str(candidate_id)}
        )
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    work = TemporalWork(run_id=run.id, target=target)
    bind(
        db,
        project_id,
        TEMPORAL_WORK_RESOURCE,
        candidate_id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [],
            "parameters": {},
            "parameter_sha256": digest({}),
            "temporal_work": work.model_dump(mode="json"),
        },
    )
    emit(db, run, "workflow.queued", {"candidate_id": str(candidate_id), "revision": run.revision})
    return run


def require_temporal_parent(db: Session, run: WorkflowRun, parent_id: UUID | None) -> None:
    if parent_id is not None:
        from arclight.persistence.agent_limits import AgentRunLimit

        control = db.get(AgentRunLimit, run.id)
        if control is None or control.parent_run_id != parent_id:
            raise DomainError("PRODUCER_DELEGATION_CONFLICT", "视频审查已有另一生产运行", 409)
