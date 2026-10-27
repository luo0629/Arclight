"""Versioned planning, explicit owner confirmation, and fixed writer derivation."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.story_outline import StoryOutlineContent
from arclight.application.commands import emit, require_episode
from arclight.application.model_services import run_settings, text_snapshot
from arclight.application.story_sources import bind_source, capture_inline, provenance
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.enums import RunStatus
from arclight.domain.project_settings import ProjectStorySettings
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    OutboxEvent,
    Project,
    StoryAnalysis,
    StoryOutline,
    StorySource,
    WorkflowRun,
)
from arclight.providers.text import build_provider

OUTLINE_VERSION = "story_outline/1.0"


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def require_outline(db: Session, project_id: UUID, version_id: UUID) -> StoryOutline:
    row = db.get(StoryOutline, version_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "分集大纲版本不存在", 404)
    return row


def idle(db: Session, episode_id: UUID) -> None:
    if db.scalar(
        select(WorkflowRun.id).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_REVIEW]),
        )
    ):
        raise DomainError("EPISODE_BUSY", "该剧集已有未完成工作流", 409)


def start_outline(
    db: Session,
    episode_id: UUID,
    actor: str,
    payload: dict,
    key: str,
    request_id: str,
    settings: Settings,
) -> WorkflowRun:
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode, with_for_update={"key_share": True})
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    digest = fingerprint({"workflow_type": "story_outline", **payload})
    existing = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.idempotency_key == key,
        )
    )
    if existing:
        if existing.request_fingerprint != digest:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求体", 409)
        return existing
    idle(db, episode_id)
    project = db.get(Project, episode.project_id)
    assert project is not None
    brief = ProjectStorySettings.model_validate(project.story_settings).model_dump()
    source_id = payload.get("source_id")
    analysis_id = payload.get("analysis_id")
    source = db.get(StorySource, UUID(source_id)) if source_id else None
    analysis = db.get(StoryAnalysis, UUID(analysis_id)) if analysis_id else None
    if source_id and (source is None or source.episode_id != episode_id):
        raise DomainError("NOT_FOUND", "故事来源不存在", 404)
    if analysis_id and (analysis is None or analysis.episode_id != episode_id):
        raise DomainError("NOT_FOUND", "提炼版本不存在", 404)
    analysis_run = db.get(WorkflowRun, analysis.run_id) if analysis else None
    story = (
        source.text
        if source
        else (analysis_run.input_json["story"] if analysis_run else payload.get("story"))
    )
    if not story or not story.strip() or len(story) > min(20000, settings.max_story_chars):
        raise DomainError(
            "STORY_OUTLINE_INPUT_INVALID", "故事不能为空，且不能超过服务长度限制", 422
        )
    if source is None and analysis and analysis.source_id:
        source = db.get(StorySource, analysis.source_id)
    if source is None:
        source = capture_inline(db, episode, actor, story)
    count = payload.get("target_episode_count") or brief["target_episode_count"]
    duration = payload.get("episode_duration_ms") or brief["default_episode_duration_ms"]
    snapshot = {
        "story": story,
        "story_sha256": hashlib.sha256(story.encode()).hexdigest(),
        "source_id": str(source.id)
        if source
        else (str(analysis.source_id) if analysis and analysis.source_id else None),
        "source_name": source.original_name
        if source
        else (analysis.source_name if analysis else "手动输入"),
        "source_sha256": source.sha256
        if source
        else (analysis.source_sha256 if analysis else None),
        "analysis_id": analysis_id,
        "analysis_version_no": analysis.version_no if analysis else None,
        "analysis": analysis.content_json if analysis else None,
        "story_settings": brief,
        "target_episode_count": count,
        "episode_duration_ms": duration,
        "source_revision": provenance(db, source),
    }
    inputs = {
        "source_snapshot": snapshot,
        "created_by": actor,
        "project_id": str(episode.project_id),
    }
    service = text_snapshot(db, actor)
    if service:
        inputs["_text_service"] = service
    run = WorkflowRun(
        episode_id=episode_id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=digest,
        workflow_type="story_outline",
        workflow_version=OUTLINE_VERSION,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    bind_source(db, run, source.id)
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="story_outline.started",
            resource_id=run.id,
            details_json={"source_id": snapshot["source_id"], "analysis_id": analysis_id},
        )
    )
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued")
    return run


def next_version(db: Session, project_id: UUID) -> int:
    return (
        db.scalar(
            select(func.max(StoryOutline.version_no)).where(StoryOutline.project_id == project_id)
        )
        or 0
    ) + 1


def generate_outline(
    factory: sessionmaker, settings: Settings, run: WorkflowRun
) -> tuple[str, int]:
    with factory.begin() as db:
        project_id = UUID(run.input_json["project_id"])
        db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
        existing = db.scalar(select(StoryOutline).where(StoryOutline.run_id == run.id))
        if existing:
            return str(existing.id), existing.version_no
        source = run.input_json["source_snapshot"]
        provider = build_provider(run_settings(settings, run.input_json.get("_text_service")))
        content = StoryOutlineContent.model_validate(provider.outline(source))
        if (
            content.target_episode_count != source["target_episode_count"]
            or content.episode_duration_ms != source["episode_duration_ms"]
        ):
            raise DomainError("OUTLINE_PLAN_MISMATCH", "大纲与请求的目标集数或时长不一致", 422)
        row = StoryOutline(
            project_id=project_id,
            episode_id=run.episode_id,
            run_id=run.id,
            version_no=next_version(db, project_id),
            source_snapshot=source,
            content_json=content.model_dump(mode="json"),
            created_by=run.input_json["created_by"],
        )
        db.add(row)
        db.flush()
        return str(row.id), row.version_no


def save_outline(
    db: Session, episode_id: UUID, actor: str, base_id: UUID, content: StoryOutlineContent, key: str
) -> StoryOutline:
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    digest = fingerprint(
        {"base_version_id": str(base_id), "content": content.model_dump(mode="json")}
    )
    existing = db.scalar(
        select(StoryOutline).where(
            StoryOutline.project_id == episode.project_id, StoryOutline.save_key == key
        )
    )
    if existing:
        if existing.request_fingerprint != digest:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求体", 409)
        return existing
    idle(db, episode_id)
    base = require_outline(db, episode.project_id, base_id)
    row = StoryOutline(
        project_id=episode.project_id,
        episode_id=base.episode_id,
        base_version_id=base.id,
        version_no=next_version(db, episode.project_id),
        content_json=content.model_dump(mode="json"),
        source_snapshot=base.source_snapshot,
        created_by=actor,
        save_key=key,
        request_fingerprint=digest,
    )
    db.add(row)
    db.flush()
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="story_outline.version_created",
            resource_id=row.id,
            details_json={"base_version_id": str(base.id)},
        )
    )
    return row


def confirm_outline(db: Session, episode_id: UUID, actor: str, version_id: UUID) -> StoryOutline:
    episode = require_episode(db, episode_id, actor, write=True, owner=True)
    db.refresh(episode)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    row = require_outline(db, episode.project_id, version_id)
    if row.status == "APPROVED":
        return row
    idle(db, episode_id)
    StoryOutlineContent.model_validate(row.content_json)
    row.status = "APPROVED"
    row.confirmed_by = actor
    row.confirmed_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="story_outline.confirmed",
            resource_id=row.id,
            details_json={},
        )
    )
    db.flush()
    return row


def writer_outline(db: Session, episode: Episode, version_id: UUID) -> dict:
    row = require_outline(db, episode.project_id, version_id)
    if row.status != "APPROVED":
        raise DomainError("OUTLINE_NOT_CONFIRMED", "请先确认指定的大纲版本", 409)
    content = StoryOutlineContent.model_validate(row.content_json)
    entry = next((item for item in content.episodes if item.episode_no == episode.episode_no), None)
    if entry is None or content.episode_duration_ms != episode.target_duration_ms:
        raise DomainError(
            "OUTLINE_EPISODE_MISMATCH",
            "当前剧集的集号或时长与所选大纲不匹配，请调整剧集或另存大纲",
            409,
        )
    return {
        "version_id": str(row.id),
        "version_no": row.version_no,
        "episode": entry.model_dump(),
        "episode_duration_ms": content.episode_duration_ms,
        "source_snapshot": row.source_snapshot,
    }
