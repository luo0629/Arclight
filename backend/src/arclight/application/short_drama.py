"""Admission and public progress for automatic short-drama production."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_script_content
from arclight.application.commands import emit, require_episode
from arclight.application.content_dependencies import content_approval_impact
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_diagnostics import public_diagnostic
from arclight.application.producer_interaction import enqueue_initial_producer_plan
from arclight.application.producer_planning import start_producer
from arclight.application.render_inputs import font_facts
from arclight.application.text_reviews import complete_manual_review, validate_manual_approval
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.short_drama import ShortDramaCreate, ShortDramaPreflight
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    Project,
    Script,
    WorkflowEvent,
    WorkflowRun,
)

PROGRESS_EVENT = "short_drama.progress"
_PUBLIC_FAILURES = frozenset(
    {
        "SHORT_DRAMA_ART_PROTECTED",
        "SHORT_DRAMA_AUDIO_COVERAGE",
        "SHORT_DRAMA_AUDIO_DURATION",
        "SHORT_DRAMA_AUDIO_REQUIRED",
        "SHORT_DRAMA_BOARD_IMPACT",
        "SHORT_DRAMA_DELIVERY_QA",
        "SHORT_DRAMA_EXPORT_WARNINGS",
        "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED",
        "SHORT_DRAMA_INPUT_CHANGED",
        "SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED",
        "SHORT_DRAMA_PLAN_BLOCKED",
        "SHORT_DRAMA_PLAN_SCOPE",
        "SHORT_DRAMA_SFX_REQUIRED",
        "SHORT_DRAMA_SHOT_LIMIT",
        "SHORT_DRAMA_SPECIALIST_BLOCKED",
        "SHORT_DRAMA_TTS_UNCERTAIN",
        "SHORT_DRAMA_VIDEO_FORMAT_UNSUPPORTED",
        "SHORT_DRAMA_VISUAL_HUMAN_REQUIRED",
        "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN",
        "SHORT_DRAMA_VISUAL_RETRY_EXHAUSTED",
        "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED",
        "SHORT_DRAMA_VOICE_REQUIRED",
    }
)


def public_short_drama_error(code: str | None) -> str | None:
    return code if code in _PUBLIC_FAILURES else public_diagnostic(code)


def require_exact_inputs(db: Session, episode: Episode, spec: dict) -> Script:
    db.refresh(episode)
    script = db.get(Script, UUID(spec["script_version_id"]), populate_existing=True)
    project = db.get(Project, episode.project_id)
    if (
        script is None
        or script.episode_id != episode.id
        or script.status != "APPROVED"
        or digest(script.content_json) != spec["script_sha256"]
        or project is None
        or project.output_profile != spec["output_profile"]
        or episode.target_duration_ms != spec["target_duration_ms"]
    ):
        raise DomainError("SHORT_DRAMA_INPUT_CHANGED", "已确认剧本或本集制作规格发生变化", 409)
    return script


def short_drama_preflight(
    db: Session, settings: Settings, episode_id: UUID, actor: str, script_version_id: UUID
) -> ShortDramaPreflight:
    from arclight.application.short_drama_audio import inspect_dialogue

    episode = require_episode(db, episode_id, actor, owner=True, write=True)
    script = db.get(Script, script_version_id)
    if script is None or script.episode_id != episode.id:
        raise DomainError("NOT_FOUND", "本集剧本版本不存在", 404)
    if script.status not in ("READY_FOR_REVIEW", "APPROVED"):
        raise DomainError("SHORT_DRAMA_SCRIPT_INVALID", "请选择可定稿的剧本", 409)
    project = db.get(Project, episode.project_id)
    assert project is not None
    needed, blocked = inspect_dialogue(db, settings, project.id, actor, script)
    return ShortDramaPreflight(needed_voices=needed, blocked_voices=blocked)


def start_short_drama(
    db: Session,
    settings: Settings,
    episode_id: UUID,
    actor: str,
    payload: ShortDramaCreate,
    key: str,
    trace_id: str,
) -> WorkflowRun:
    # Same lock order as ordinary Producer admission and accounting.
    lock_gate(db)
    episode = require_episode(db, episode_id, actor, owner=True, write=True)
    db.refresh(episode, with_for_update=True)
    request_hash = digest(payload.model_dump(mode="json"))
    previous = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode.id,
            WorkflowRun.idempotency_key == key,
        )
    )
    if previous:
        if (
            previous.workflow_type != "producer"
            or previous.input_json.get("created_by") != actor
            or previous.input_json.get("short_drama", {}).get("request_sha256") != request_hash
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一请求不能更换制作参数", 409)
        return previous
    project = db.get(Project, episode.project_id)
    assert project is not None
    script = db.get(Script, payload.script_version_id, with_for_update=True)
    if script is None or script.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "本集剧本版本不存在", 404)
    if script.status not in ("READY_FOR_REVIEW", "APPROVED"):
        raise DomainError("SHORT_DRAMA_SCRIPT_INVALID", "请选择可定稿的剧本", 409)
    if script.status == "READY_FOR_REVIEW" and script.run_id is not None:
        writer = db.get(WorkflowRun, script.run_id)
        if writer is None or writer.status != "SUCCEEDED":
            raise DomainError("SHORT_DRAMA_SCRIPT_INVALID", "请等待编剧完成当前草稿", 409)
    validate_manual_approval(db, script, settings)
    content = read_script_content(script.content_json)
    font_facts(
        db,
        project.id,
        payload.font_file_id,
        required=any(scene.dialogue for scene in content.scenes),
    )
    if not payload.generated_media_rights_confirmed:
        raise DomainError("SHORT_DRAMA_RIGHTS_REQUIRED", "请确认本次生成素材可用于此短剧", 409)
    if script.status == "READY_FOR_REVIEW":
        impact = content_approval_impact(db, script)
        if impact["affected"] and payload.impact_digest is None:
            raise DomainError("DEPENDENCY_IMPACT_REQUIRED", "请预览并确认剧本变更影响", 409)
        complete_manual_review(
            db,
            script,
            actor,
            settings,
            decision="APPROVE",
            comment="确认此剧本并授权自动制作短剧",
            impact_digest=payload.impact_digest,
        )
    frozen = {
        "script_version_id": str(script.id),
        "script_sha256": digest(script.content_json),
        "request_sha256": request_hash,
        "target_duration_ms": episode.target_duration_ms,
        "output_profile": project.output_profile,
        "font_file_id": str(payload.font_file_id) if payload.font_file_id else None,
        "generated_media_rights_confirmed": payload.generated_media_rights_confirmed,
    }
    run = start_producer(
        db,
        settings,
        episode.id,
        actor,
        (
            f"根据已定稿剧本 {script.id} 制作本集短剧。"
            "只规划美术指导和导演两项专业任务，两者都引用上述精确剧本。"
            "导演依赖美术任务。美术仅补充未受保护的设计，不指定已有目标资产、"
            "显式设置美术任务的 minimum_reference_images 为 0，也不委派美术概念图生成。"
            "导演无需另行召集美术协作。"
            "剧本已由所有者确认，无需重新编剧。镜头生成和成片由服务继续执行。"
        ),
        key,
        trace_id,
        short_drama=frozen,
    )
    from arclight.application.short_drama_audio import preflight_dialogue

    preflight_dialogue(db, settings, run, project.id, actor, script.id)
    enqueue_initial_producer_plan(db, run, episode, actor)
    db.add(
        AuditEvent(
            project_id=project.id,
            actor_id=actor,
            resource_id=run.id,
            action="short_drama.authorized",
            details_json=frozen,
        )
    )
    emit(db, run, PROGRESS_EVENT, {"phase": "PLANNING"})
    return run


def progress_for(db: Session, run: WorkflowRun) -> dict:
    latest = db.scalar(
        select(WorkflowEvent)
        .where(WorkflowEvent.run_id == run.id, WorkflowEvent.event_type == PROGRESS_EVENT)
        .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.id.desc())
        .limit(1)
    )
    return latest.payload_json if latest else {"phase": "PLANNING"}


def read_short_drama(db: Session, episode_id: UUID, actor: str) -> dict | None:
    episode = require_episode(db, episode_id, actor)
    run = db.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.workflow_type == "producer",
            WorkflowRun.input_json["short_drama"]["script_version_id"].as_string().is_not(None),
        )
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(1)
    )
    if run is None:
        return None
    from arclight.application.short_drama_production import balance_blocker

    return {
        "run_id": run.id,
        "status": run.status,
        "revision": run.revision,
        "can_retry_balance": run.input_json.get("created_by") == actor
        and balance_blocker(db, run, episode.project_id) is not None,
        "script_version_id": run.input_json["short_drama"]["script_version_id"],
        "error_code": public_short_drama_error(run.error_code),
        **progress_for(db, run),
    }
