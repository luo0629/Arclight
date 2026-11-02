"""Independent, immutable story extraction; no script/asset creation."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.application.commands import emit, require_episode
from arclight.application.model_services import run_settings, text_snapshot
from arclight.application.source_objects import source_text
from arclight.application.story_sources import bind_source, capture_inline, provenance
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.enums import RunStatus
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    OutboxEvent,
    StoryAnalysis,
    StorySource,
    WorkflowRun,
)
from arclight.providers.text import build_provider

ANALYSIS_VERSION = "story_analysis/1.0"


def start_analysis(
    db: Session,
    episode_id: UUID,
    actor: str,
    story: str | None,
    source_id: UUID | None,
    key: str,
    request_id: str,
    settings: Settings,
) -> WorkflowRun:
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode, with_for_update={"key_share": True})
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "workflow_type": "story_analysis",
                "story": story,
                "source_id": str(source_id) if source_id else None,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
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
    source = db.get(StorySource, source_id) if source_id else None
    if source_id and (source is None or source.episode_id != episode_id):
        raise DomainError("NOT_FOUND", "故事来源不存在", 404)
    text = source_text(db, settings, source) if source else story
    if text is None or not text.strip() or len(text) > min(20000, settings.max_story_chars):
        raise DomainError(
            "STORY_ANALYSIS_INPUT_INVALID",
            "故事不能为空，且不能超过服务限制（最多20000字）；请缩短后重试",
            422,
        )
    if source is None:
        source = capture_inline(db, episode, actor, text, settings=settings)
    inputs: dict = {
        "story": text,
        "story_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_id": str(source.id) if source else None,
        "source_name": source.original_name if source_id else "手动输入",
        "source_sha256": source.sha256 if source else None,
        "created_by": actor,
        "source_revision": provenance(db, source),
    }
    snapshot = text_snapshot(db, actor)
    if snapshot:
        inputs["_text_service"] = snapshot
    run = WorkflowRun(
        episode_id=episode_id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        workflow_type="story_analysis",
        workflow_version=ANALYSIS_VERSION,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    from arclight.application.production_versions import capture_text

    capture_text(db, episode.project_id, run, settings, actor=actor)
    bind_source(db, run, source.id)
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="story_analysis.started",
            resource_id=run.id,
            details_json={"source_id": inputs["source_id"]},
        )
    )
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued")
    return run


def extract(factory: sessionmaker, settings: Settings, run: WorkflowRun) -> tuple[str, int]:
    # Commit the artifact separately so a failed terminal update can replay without billing again.
    with factory.begin() as db:
        db.execute(
            select(Episode.id).where(Episode.id == run.episode_id).with_for_update(key_share=True)
        )
        existing = db.scalar(select(StoryAnalysis).where(StoryAnalysis.run_id == run.id))
        if existing:
            return str(existing.id), existing.version_no
        inputs = run.input_json
        from arclight.application.context_snapshots import read_contexts
        from arclight.providers.context import bind_contexts

        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        provider = bind_contexts(
            build_provider(run_settings(settings, inputs.get("_text_service"))),
            read_contexts(db, episode.project_id, run),
        )
        content = StoryAnalysisContent.model_validate(provider.analyze(inputs["story"]))
        version = (
            db.scalar(
                select(func.max(StoryAnalysis.version_no)).where(
                    StoryAnalysis.episode_id == run.episode_id
                )
            )
            or 0
        ) + 1
        row = StoryAnalysis(
            episode_id=run.episode_id,
            run_id=run.id,
            version_no=version,
            source_id=UUID(inputs["source_id"]) if inputs["source_id"] else None,
            source_name=inputs["source_name"],
            source_sha256=inputs["source_sha256"],
            story_sha256=inputs["story_sha256"],
            created_by=inputs["created_by"],
            content_json=content.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
        from arclight.application.production_versions import bind_output

        bind_output(
            db, db.get(Episode, run.episode_id).project_id, "WORKFLOW", run.id, "ANALYSIS", row.id
        )
        return str(row.id), version
