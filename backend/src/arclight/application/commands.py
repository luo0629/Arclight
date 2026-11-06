import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.api.schemas import ReviewCreate
from arclight.application.content_duration import validate_duration
from arclight.application.content_references import (
    validate_script_assets,
    validate_storyboard_assets,
)
from arclight.application.model_services import text_snapshot
from arclight.application.source_objects import source_text
from arclight.application.story_context import build_story_context
from arclight.application.story_sources import (
    bind_source,
    capture_inline,
    provenance,
    require_source,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.enums import AssetStatus, RunStatus
from arclight.domain.project_settings import ProjectStorySettings
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    OutboxEvent,
    Project,
    ProjectMember,
    Review,
    Script,
    Shot,
    Storyboard,
    StorySource,
    WorkflowEvent,
    WorkflowRun,
)
from arclight.workflows.production import DIRECTOR_WORKFLOW_VERSION, WORKFLOW_VERSION


def require_project(
    db: Session, project_id: UUID, actor: str, *, write: bool = False, owner: bool = False
) -> Project:
    # Refresh after the shared write gate to avoid stale pre-deletion ORM state.
    project = db.get(
        Project, project_id, populate_existing=write or owner, with_for_update=write or owner
    )
    if project is None or project.deleted_at is not None:
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
    if event_type in {
        "review.submitted",
        "workflow.waiting_review",
        "workflow.finished",
        "workflow.failed",
        "workflow.cancelled",
        "workflow.retry_requested",
    }:
        from arclight.application.production_events import enqueue_text

        enqueue_text(db, run)


def start_run(
    db: Session,
    episode_id: UUID,
    actor: str,
    story: str,
    key: str,
    request_id: str,
    *,
    settings: Settings,
    script_version_id: UUID | None = None,
    outline_version_id: UUID | None = None,
    source_id: UUID | None = None,
    rebuild_from_version_id: UUID | None = None,
) -> WorkflowRun:
    episode = require_episode(db, episode_id, actor, write=True)
    # 同一剧集写入串行化，同时保护版本号分配；唯一约束作为最终防线。
    db.refresh(episode, with_for_update={"key_share": True})
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    project = require_project(db, episode.project_id, actor)
    # 旧请求指纹保持不变；两种启动方式共享剧集内的幂等键空间。
    payload = (
        {"story": story}
        if script_version_id is None
        else {"script_version_id": str(script_version_id)}
    )
    if outline_version_id is not None:
        payload = {"outline_version_id": str(outline_version_id)}
    if source_id is not None:
        payload = {"source_id": str(source_id)}
    if rebuild_from_version_id is not None:
        payload["rebuild_from_version_id"] = str(rebuild_from_version_id)
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
    if rebuild_from_version_id is not None:
        original = db.get(Storyboard, rebuild_from_version_id)
        if original is None or original.episode_id != episode_id:
            raise DomainError("NOT_FOUND", "待重建分镜不存在", 404)
        if original.status != "STALE" or script_version_id is None:
            raise DomainError(
                "INVALID_REBUILD_INPUT", "只允许按需重生成失效分镜并指定批准剧本", 409
            )
    from arclight.application.visual_bible import snapshot as visual_snapshot

    inputs: dict = {
        "story": story,
        "style_guide": project.style_guide,
        "visual_bible": visual_snapshot(db, project),
        "story_settings": ProjectStorySettings.model_validate(project.story_settings).model_dump(),
        "target_duration_ms": episode.target_duration_ms,
    }
    if script_version_id is None:
        db.execute(select(Project.id).where(Project.id == project.id).with_for_update())
        inputs["story_context"] = build_story_context(db, project.id)
    if script_version_id is not None:
        script = db.get(Script, script_version_id)
        if script is None or script.episode_id != episode_id:
            raise DomainError("NOT_FOUND", "指定剧本版本不存在", 404)
        if script.status != AssetStatus.APPROVED:
            raise DomainError("SCRIPT_NOT_APPROVED", "只能从已批准的剧本版本生成分镜")
        source = ScriptContent.model_validate(script.content_json)
        validate_script_assets(db, project.id, source)
        validate_duration(
            source, episode.target_duration_ms, settings.text_duration_tolerance_percent
        )
        # 固定精确版本和正文快照，不在 Worker 执行时重新选择“最新版”。
        inputs = {
            "script_version_id": str(script.id),
            "script": script.content_json,
            "target_duration_ms": episode.target_duration_ms,
        }
    if outline_version_id is not None:
        from arclight.application.story_outline import writer_outline

        outline = writer_outline(db, episode, outline_version_id)
        inputs["outline_context"] = outline
        inputs["story"] = outline["source_snapshot"]["story"]
    story_source = None
    if script_version_id is None:
        if source_id is not None:
            story_source = require_source(db, episode_id, source_id)
            inputs["story"] = source_text(db, settings, story_source)
        elif outline_version_id is not None:
            fixed_source = inputs["outline_context"]["source_snapshot"].get("source_id")
            if fixed_source:
                story_source = db.get(StorySource, UUID(fixed_source))
                origin_episode = db.get(Episode, story_source.episode_id) if story_source else None
                if origin_episode is None or origin_episode.project_id != project.id:
                    raise DomainError("NOT_FOUND", "大纲来源不存在", 404)
        if not inputs["story"] or len(inputs["story"]) > min(20000, settings.max_story_chars):
            raise DomainError("STORY_TOO_LARGE", "故事不能为空且不能超过服务长度限制", 422)
        if story_source is None:
            story_source = capture_inline(db, episode, actor, inputs["story"], settings=settings)
        inputs["source_revision"] = provenance(db, story_source)
    snapshot = text_snapshot(db, actor)
    if rebuild_from_version_id is not None:
        inputs["rebuild_from_version_id"] = str(rebuild_from_version_id)
    if snapshot:
        inputs["_text_service"] = snapshot
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
    from arclight.application.agent_runtime_state import initialize_run_state

    initialize_run_state(run, episode.project_id)
    from arclight.application.production_versions import capture_text

    capture_text(db, episode.project_id, run, settings, actor=actor)
    if story_source is not None:
        bind_source(db, run, story_source.id)
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="workflow.started",
            resource_id=run.id,
            details_json={},
        )
    )
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued")
    return run


def resume_run(
    db: Session, run: WorkflowRun, actor: str, review: ReviewCreate, settings: Settings
) -> WorkflowRun:
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
    from arclight.application.human_reviews import record_resume
    from arclight.application.text_reviews import prepare_text_decision

    if review.decision == "APPROVE":
        episode = require_episode(db, run.episode_id, actor)
        if isinstance(artifact, Script):
            validate_script_assets(
                db, episode.project_id, ScriptContent.model_validate(artifact.content_json)
            )
        else:
            validate_storyboard_assets(
                db, episode.project_id, StoryboardContent.model_validate(artifact.content_json)
            )
        parsed = (
            ScriptContent if isinstance(artifact, Script) else StoryboardContent
        ).model_validate(artifact.content_json)
        validate_duration(
            parsed, run.input_json["target_duration_ms"], settings.text_duration_tolerance_percent
        )
    review_request, review_decision = prepare_text_decision(
        db, artifact, actor, review.decision, review.comment, review.impact_digest
    )
    artifact.status = AssetStatus.APPROVED if review.decision == "APPROVE" else AssetStatus.REJECTED
    if isinstance(artifact, Storyboard):
        for shot in db.scalars(select(Shot).where(Shot.storyboard_id == artifact.id)):
            shot.status = artifact.status
    from arclight.application.content_dependencies import review_registry

    review_registry(db, artifact, actor, review.impact_digest)
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
    record_resume(
        db,
        review_request,
        review_decision,
        actor,
        {
            "run_id": str(run.id),
            "revision": run.revision,
            "thread_id": str(run.id),
            "status": "QUEUED",
        },
    )
    return run


def cancel_run(db: Session, run: WorkflowRun) -> WorkflowRun:
    if run.status not in [RunStatus.QUEUED, RunStatus.WAITING_REVIEW]:
        raise DomainError("NOT_CANCELLABLE", "仅排队或等待审核的工作流可以取消")
    agent_state = None
    if run.agent_state_json is not None:
        from arclight.domain.agent_state import AgentStateError, terminate_agent_state

        try:
            episode = db.get(Episode, run.episode_id)
            if episode is None:
                raise AgentStateError("Run episode is missing")
            from arclight.application.production_versions import input_summary
            from arclight.domain.agent_state import AgentRunScope
            from arclight.domain.versions import digest

            scope = AgentRunScope(
                run_id=run.id,
                project_id=episode.project_id,
                workflow_type=run.workflow_type,
                workflow_version=run.workflow_version,
                input_sha256=digest(input_summary(run.input_json)),
            )
            state = terminate_agent_state(
                run.agent_state_json,
                scope,
                reason="CANCELLED",
            )
        except AgentStateError as exc:
            raise DomainError(
                "AGENT_STATE_INVALID", "运行状态不可安全取消，请联系管理员", 409
            ) from exc
        agent_state = state.model_dump(mode="json")
    run.status = RunStatus.CANCELLED
    run.pending_review = None
    if agent_state is not None:
        run.agent_state_json = agent_state
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
    if run.agent_state_json is not None:
        from arclight.domain.agent_state import AgentStateError, retry_agent_state

        try:
            state = retry_agent_state(run.agent_state_json)
        except AgentStateError as exc:
            raise DomainError(
                "AGENT_STATE_INVALID", "运行状态不可安全重试，请联系管理员", 409
            ) from exc
        run.agent_state_json = state.model_dump(mode="json")
    run.status = RunStatus.QUEUED
    run.error_code = None
    run.revision += 1
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(db, run, "workflow.retry_requested")
    return run
