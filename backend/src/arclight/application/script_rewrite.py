"""Start a version-bound rewrite; provider calls happen only in the worker."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContentV3, read_script_content
from arclight.agents.script_rewrite import RewriteRequest, RewriteResult
from arclight.application.commands import emit, require_episode, require_project
from arclight.application.content_duration import duration_bounds, validate_duration
from arclight.application.content_references import validate_script_assets
from arclight.application.model_services import require_text_snapshot
from arclight.application.rewrite_content import content_hash, merge_rewrite, provider_context
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.enums import RunStatus
from arclight.domain.screenwriter_revision import (
    REWRITE_SCREENWRITER_VERSION,
    SERIES_REWRITE_SCREENWRITER_VERSION,
    ScreenwriterRevisionOptions,
)
from arclight.domain.script_drafts import SCRIPT_REWRITE_DRAFT, ScriptWritingPurpose
from arclight.domain.script_narrative import NarrativeRequest
from arclight.persistence.models import AuditEvent, OutboxEvent, Script, WorkflowRun
from arclight.providers.text import build_provider
from arclight.workflows.rewrite import REWRITE_VERSION


def start_rewrite(
    db: Session,
    episode_id: UUID,
    actor: str,
    request: RewriteRequest,
    key: str,
    request_id: str,
    settings: Settings,
    *,
    purpose: ScriptWritingPurpose = "REVIEW_WORKFLOW",
    professional: bool = False,
    agent_limits: AgentRunLimits | None = None,
    revision_rounds: int | None = None,
    narrative: NarrativeRequest | None = None,
) -> WorkflowRun:
    options = ScreenwriterRevisionOptions(
        professional=professional, agent_limits=agent_limits, revision_rounds=revision_rounds
    )
    if narrative is not None and not professional:
        raise DomainError("SCREENWRITER_TASK_INVALID", "连续剧情改写须选择专业编剧", 422)
    if professional:
        from arclight.application.model_gateway import lock_gate

        lock_gate(db)
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode, with_for_update={"key_share": True})
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    workflow_type = SCRIPT_REWRITE_DRAFT if purpose == "EDITABLE_DRAFT" else "script_rewrite"
    payload = {"workflow_type": workflow_type, **request.model_dump(mode="json")}
    if professional:
        payload |= {
            "professional": True,
            "agent_limits": options.frozen_limits().model_dump(mode="json"),
            "revision_rounds": revision_rounds if revision_rounds is not None else 3,
        }
    if narrative is not None:
        payload["narrative"] = narrative.model_dump(mode="json")
    fingerprint = content_hash(payload)
    existing = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id, WorkflowRun.idempotency_key == key
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求体", 409)
        return existing
    if db.scalar(
        select(WorkflowRun.id).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_REVIEW]),
        )
    ):
        raise DomainError("EPISODE_BUSY", "该剧集已有未完成工作流", 409)
    base = db.get(Script, request.base_version_id)
    if base is None or base.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "来源剧本版本不存在", 404)
    if base.status not in {"READY_FOR_REVIEW", "APPROVED", "REJECTED"}:
        raise DomainError("INVALID_REWRITE_SOURCE", "此版本不能用于新改写", 409)
    source = read_script_content(base.content_json)
    if isinstance(source, ScriptContentV3) and narrative is None:
        raise DomainError(
            "NARRATIVE_CONTEXT_REQUIRED", "连续剧情改写须明确选择新的精确叙事基准", 422
        )
    if narrative is not None and not isinstance(source, ScriptContentV3):
        raise DomainError(
            "LEGACY_SCRIPT_READ_ONLY", "旧剧本未记录场景叙事交付，请经人工另存完整3.0版本", 409
        )
    if narrative is not None:
        from arclight.application.rewrite_content import selected_scenes

        assert isinstance(source, ScriptContentV3)
        selected_ids = {scene.scene_id for scene in selected_scenes(source, request.target)}
        anchor = next(
            (scene for scene in source.scenes if scene.scene_no == narrative.target_scene_no),
            None,
        )
        if anchor is None or (
            anchor.scene_id not in selected_ids
            and anchor.dramatic_beat.story_time != narrative.target_story_time
        ):
            raise DomainError(
                "NARRATIVE_TARGET_INVALID", "写作锚点须存在，范围外锚点须保持原稿故事时点", 422
            )
    validate_script_assets(db, episode.project_id, source)
    validate_duration(source, episode.target_duration_ms, settings.text_duration_tolerance_percent)
    project = require_project(db, episode.project_id, actor)
    from arclight.application.visual_bible import prompt_style
    from arclight.application.visual_bible import snapshot as visual_snapshot

    visual = visual_snapshot(db, project)

    context = {
        **provider_context(source, request.target),
        "instructions": request.instructions.model_dump(),
        "style_guide": prompt_style(db, project, visual),
        "visual_bible": visual,
        "story_settings": project.story_settings,
        "target_duration_ms": episode.target_duration_ms,
        "duration_range_ms": duration_bounds(
            episode.target_duration_ms, settings.text_duration_tolerance_percent
        ),
    }
    if len(json.dumps(context, ensure_ascii=False)) > min(60000, settings.max_story_chars):
        raise DomainError("REWRITE_INPUT_TOO_LARGE", "改写上下文超出服务限制，请缩小选定范围", 422)
    inputs: dict = {
        "target_duration_ms": episode.target_duration_ms,
        "rewrite_input": {
            "request": request.model_dump(mode="json"),
            "source_script": source.model_dump(mode="json"),
            "source_sha256": content_hash(base.content_json),
            "base_version_no": base.version_no,
            "context": context,
        },
    }
    if narrative is not None:
        from arclight.application.writing_context import capture_narrative_context

        inputs["narrative_context"] = capture_narrative_context(db, episode, actor, narrative)
    service = require_text_snapshot(db, actor, settings)
    if service:
        inputs["_text_service"] = service
    if professional:
        inputs["screenwriter"] = {
            "agent_limits": options.frozen_limits().model_dump(mode="json"),
            "revision_rounds": revision_rounds if revision_rounds is not None else 3,
        }
    run = WorkflowRun(
        episode_id=episode_id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        workflow_type=workflow_type,
        workflow_version=SERIES_REWRITE_SCREENWRITER_VERSION
        if narrative is not None
        else REWRITE_SCREENWRITER_VERSION
        if professional
        else REWRITE_VERSION,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    from arclight.application.agent_runtime_state import initialize_run_state

    initialize_run_state(run, episode.project_id)
    from arclight.application.production_versions import capture_text

    capture_text(db, episode.project_id, run, settings, actor=actor)
    if professional:
        from arclight.application.screenwriter_creation import authorize_screenwriter

        authorize_screenwriter(db, run, actor, settings)
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="workflow.started",
            resource_id=run.id,
            details_json={
                "base_version_id": str(base.id),
                "scope": request.target.scope,
            },
        )
    )
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued")
    return run


def generate_rewrite(inputs: dict, settings: Settings, contexts: dict | None = None) -> dict:
    from arclight.providers.context import bind_contexts

    provider = bind_contexts(build_provider(settings), contexts or {})
    patch = RewriteResult.model_validate(provider.rewrite(inputs["context"]))
    return merge_rewrite(inputs, patch).model_dump(mode="json")
