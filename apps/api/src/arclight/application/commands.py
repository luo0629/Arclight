import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.api.schemas import ReviewCreate
from arclight.core.errors import DomainError
from arclight.domain.enums import AssetStatus, RunStatus
from arclight.persistence.models import (
    Episode,
    OutboxEvent,
    Project,
    ProjectMember,
    Review,
    Script,
    Shot,
    Storyboard,
    WorkflowEvent,
    WorkflowRun,
)
from arclight.workflows.production import DIRECTOR_WORKFLOW_VERSION, WORKFLOW_VERSION


def require_project(
    db: Session, project_id: UUID, actor: str, *, write: bool = False, owner: bool = False
) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
    if project.owner_id != actor:
        membership = (
            None
            if actor == "service-owner"
            else db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == UUID(actor)
                )
            )
        )
        if membership is None:
            raise DomainError("NOT_FOUND", "项目不存在", 404)
        if owner or (write and membership.role != "editor"):
            raise DomainError("FORBIDDEN", "当前项目角色没有此操作权限", 403)
    if write and project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目只允许查看", 409)
    return project


def require_episode(
    db: Session, episode_id: UUID, actor: str, *, write: bool = False, owner: bool = False
) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise DomainError("NOT_FOUND", "剧集不存在", 404)
    require_project(db, episode.project_id, actor, write=write, owner=owner)
    if write and episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    return episode


def require_run(db: Session, run_id: UUID, actor: str, *, lock: bool = False) -> WorkflowRun:
    statement = select(WorkflowRun).where(WorkflowRun.id == run_id)
    if lock:
        statement = statement.with_for_update(key_share=True)
    run = db.scalar(statement)
    if run is None:
        raise DomainError("NOT_FOUND", "工作流不存在", 404)
    require_episode(db, run.episode_id, actor, write=lock)
    return run


def emit(db: Session, run: WorkflowRun, event_type: str, payload: dict | None = None) -> None:
    db.add(WorkflowEvent(run_id=run.id, event_type=event_type, payload_json=payload or {}))


def start_run(
    db: Session,
    episode_id: UUID,
    actor: str,
    story: str,
    key: str,
    request_id: str,
    *,
    script_version_id: UUID | None = None,
) -> WorkflowRun:
    episode = require_episode(db, episode_id, actor, write=True)
    # 同一剧集写入串行化，同时保护版本号分配；唯一约束作为最终防线。
    db.execute(select(Episode).where(Episode.id == episode_id).with_for_update(key_share=True))
    project = require_project(db, episode.project_id, actor)
    # 旧请求指纹保持不变；两种启动方式共享剧集内的幂等键空间。
    payload = (
        {"story": story}
        if script_version_id is None
        else {"script_version_id": str(script_version_id)}
    )
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id, WorkflowRun.idempotency_key == key
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求体")
        return existing
    active = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_REVIEW]),
        )
    )
    if active:
        raise DomainError("EPISODE_BUSY", "该剧集已有未完成工作流")
    inputs = {
        "story": story,
        "style_guide": project.style_guide,
        "target_duration_ms": episode.target_duration_ms,
    }
    if script_version_id is not None:
        script = db.get(Script, script_version_id)
        if script is None or script.episode_id != episode_id:
            raise DomainError("NOT_FOUND", "指定剧本版本不存在", 404)
        if script.status != AssetStatus.APPROVED:
            raise DomainError("SCRIPT_NOT_APPROVED", "只能从已批准的剧本版本生成分镜")
        # 固定精确版本和正文快照，不在 Worker 执行时重新选择“最新版”。
        inputs = {
            "script_version_id": str(script.id),
            "script": script.content_json,
            "target_duration_ms": episode.target_duration_ms,
        }
    run = WorkflowRun(
        episode_id=episode_id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        workflow_type="script_storyboard" if script_version_id is None else "storyboard",
        workflow_version=WORKFLOW_VERSION
        if script_version_id is None
        else DIRECTOR_WORKFLOW_VERSION,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued")
    return run


def resume_run(db: Session, run: WorkflowRun, actor: str, review: ReviewCreate) -> WorkflowRun:
    require_episode(db, run.episode_id, actor, write=True, owner=True)
    if run.status != RunStatus.WAITING_REVIEW or not run.pending_review:
        raise DomainError("NOT_WAITING_REVIEW", "当前工作流不在人工审核节点")
    if run.revision != review.expected_revision:
        raise DomainError("STALE_REVIEW", "工作流已变化，请刷新后重新审核")
    expected = {"resource_type": review.resource_type, "version_id": str(review.version_id)}
    if run.pending_review != expected:
        raise DomainError("STALE_REVIEW", "审核对象与当前待审核版本不一致")
    artifact: Script | Storyboard | None
    if review.resource_type == "script":
        artifact = db.get(Script, review.version_id)
    else:
        artifact = db.get(Storyboard, review.version_id)
    if artifact is None or artifact.run_id != run.id:
        raise DomainError("STALE_REVIEW", "待审核版本不存在或不属于本次工作流")
    artifact.status = AssetStatus.APPROVED if review.decision == "APPROVE" else AssetStatus.REJECTED
    if isinstance(artifact, Storyboard):
        for shot in db.scalars(select(Shot).where(Shot.storyboard_id == artifact.id)):
            shot.status = artifact.status
    db.add(
        Review(
            run_id=run.id,
            resource_type=review.resource_type,
            version_id=review.version_id,
            reviewer_id=actor,
            decision=review.decision,
            comment=review.comment,
        )
    )
    run.resume_json = review.model_dump(mode="json")
    run.pending_review = None
    run.status = RunStatus.QUEUED
    run.revision += 1
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(db, run, "review.submitted", review.model_dump(mode="json"))
    return run


def cancel_run(db: Session, run: WorkflowRun) -> WorkflowRun:
    if run.status not in [RunStatus.QUEUED, RunStatus.WAITING_REVIEW]:
        raise DomainError("NOT_CANCELLABLE", "仅排队或等待审核的工作流可以取消")
    run.status = RunStatus.CANCELLED
    run.pending_review = None
    run.revision += 1
    emit(db, run, "workflow.cancelled")
    return run


def retry_run(db: Session, run: WorkflowRun) -> WorkflowRun:
    if run.status != RunStatus.FAILED:
        raise DomainError("NOT_RETRYABLE", "只有失败的工作流可以显式重试")
    db.execute(select(Episode).where(Episode.id == run.episode_id).with_for_update(key_share=True))
    active = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == run.episode_id,
            WorkflowRun.id != run.id,
            WorkflowRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_REVIEW]),
        )
    )
    if active:
        raise DomainError("EPISODE_BUSY", "该剧集已有其他未完成工作流")
    # 此重试由用户主动请求；模型超时不触发隐式付费重提。
    run.status = RunStatus.QUEUED
    run.error_code = None
    run.revision += 1
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(db, run, "workflow.retry_requested")
    return run
