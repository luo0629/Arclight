"""Start a version-bound rewrite; provider calls happen only in the worker."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.agents.script_rewrite import RewriteRequest, RewriteResult
from arclight.application.commands import emit, require_episode, require_project
from arclight.application.content_duration import duration_bounds, validate_duration
from arclight.application.content_references import validate_script_assets
from arclight.application.model_services import text_snapshot
from arclight.application.rewrite_content import content_hash, merge_rewrite, provider_context
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.enums import RunStatus
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
) -> WorkflowRun:
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode, with_for_update={"key_share": True})
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    fingerprint = content_hash(
        {"workflow_type": "script_rewrite", **request.model_dump(mode="json")}
    )
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
    source = ScriptContent.model_validate(base.content_json)
    validate_script_assets(db, episode.project_id, source)
    validate_duration(source, episode.target_duration_ms, settings.text_duration_tolerance_percent)
    project = require_project(db, episode.project_id, actor)
    context = {
        **provider_context(source, request.target),
        "instructions": request.instructions.model_dump(),
        "style_guide": project.style_guide,
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
    service = text_snapshot(db, actor)
    if service:
        inputs["_text_service"] = service
    run = WorkflowRun(
        episode_id=episode_id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        workflow_type="script_rewrite",
        workflow_version=REWRITE_VERSION,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
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


def generate_rewrite(inputs: dict, settings: Settings) -> dict:
    provider = build_provider(settings)
    patch = RewriteResult.model_validate(provider.rewrite(inputs["context"]))
    return merge_rewrite(inputs, patch).model_dump(mode="json")
