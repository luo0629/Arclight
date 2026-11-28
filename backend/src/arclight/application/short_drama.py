"""Admission and public progress for automatic short-drama production."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import read_script_content
from arclight.application.audio_voice_catalog import query_voice_catalog
from arclight.application.commands import emit, require_episode
from arclight.application.content_dependencies import content_approval_impact
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import require_text_snapshot, text_snapshot
from arclight.application.producer_interaction import (
    enqueue_initial_producer_plan,
    pending_commands,
)
from arclight.application.producer_planning import start_producer
from arclight.application.render_inputs import bundled_font_receipt, font_facts
from arclight.application.render_manifests import output_profile
from arclight.application.short_drama_diagnostics import (
    DETAIL,
    PROGRESS_EVENT,
    ShortDramaFailure,
    public_issue,
    public_short_drama_error,
    text_failure,
)
from arclight.application.text_reviews import complete_manual_review, validate_manual_approval
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.producer_interaction import VOICE_CASTING_ACTION
from arclight.domain.short_drama import ShortDramaCreate, ShortDramaPreflight, ShortDramaTask
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    Project,
    Script,
    WorkflowEvent,
    WorkflowRun,
)


def require_short_drama_text(db: Session, settings: Settings, actor: str) -> None:
    try:
        service = require_text_snapshot(db, actor, settings)
    except DomainError as error:
        if error.code == "SERVICE_INCOMPLETE":
            raise text_failure(
                DomainError("SHORT_DRAMA_TEXT_SERVICE_REQUIRED", "请配置账号文字模型", 422),
                actor,
                None,
            ) from error
        raise text_failure(error, actor, text_snapshot(db, actor)) from error
    if service is None:  # The application's deterministic mock has no account configuration.
        return
    declaration = service.get("agent_capabilities")
    capabilities = AgentModelCapabilities.model_validate(declaration) if declaration else None
    if (
        capabilities is None
        or not capabilities.tools
        or service["response_format"] not in capabilities.response_formats
    ):
        raise text_failure(
            DomainError(
                "SHORT_DRAMA_TEXT_CAPABILITY_REQUIRED", "短剧编排需要工具调用和结构化输出能力", 422
            ),
            actor,
            service,
        )
    if capabilities.max_input_images < 1:
        raise text_failure(
            DomainError("SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED", "镜头质检需要图片理解能力", 422),
            actor,
            service,
            review=True,
        )


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
    factory: sessionmaker[Session],
    settings: Settings,
    episode_id: UUID,
    actor: str,
    script_version_id: UUID,
) -> ShortDramaPreflight:
    from arclight.application.short_drama_audio import inspect_dialogue, require_casting_options
    from arclight.application.short_drama_visuals import require_image_service

    with factory() as db:
        episode = require_episode(db, episode_id, actor, owner=True, write=True)
        script = db.get(Script, script_version_id)
        if script is None or script.episode_id != episode.id:
            raise DomainError("NOT_FOUND", "本集剧本版本不存在", 404)
        if script.status not in ("READY_FOR_REVIEW", "APPROVED"):
            raise DomainError("SHORT_DRAMA_SCRIPT_INVALID", "请选择可定稿的剧本", 409)
        project = db.get(Project, episode.project_id)
        assert project is not None
        issues = []
        try:
            require_short_drama_text(db, settings, actor)
        except ShortDramaFailure as error:
            phase = "SHOTS" if error.service_kind == "REVIEW" else "PLANNING"
            issues.append(public_issue(error.record(phase), actor))
        try:
            require_image_service(
                db, settings, project.id, actor, output_profile(project.output_profile, "FORMAL")
            )
        except ShortDramaFailure as error:
            issues.append(public_issue(error.record("SHOTS"), actor))
        needed, blocked, audio_failures, source = inspect_dialogue(
            db, settings, project.id, actor, script
        )
        issues.extend(public_issue(error.record("AUDIO"), actor) for error in audio_failures)
    if source is not None:
        assert source.selection is not None
        try:
            require_casting_options(query_voice_catalog(source), source.selection)
        except ShortDramaFailure as error:
            issues.append(public_issue(error.record("AUDIO"), actor))
    return ShortDramaPreflight(needed_voices=needed, blocked_voices=blocked, issues=issues)


def _existing_short_drama(
    db: Session, episode_id: UUID, actor: str, key: str, request_hash: str
) -> WorkflowRun | None:
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.idempotency_key == key,
        )
    )
    if run is not None and (
        run.workflow_type != "producer"
        or run.input_json.get("created_by") != actor
        or run.input_json.get("short_drama", {}).get("request_sha256") != request_hash
    ):
        raise DomainError("IDEMPOTENCY_CONFLICT", "同一请求不能更换制作参数", 409)
    return run


def start_short_drama(
    factory: sessionmaker[Session],
    settings: Settings,
    episode_id: UUID,
    actor: str,
    payload: ShortDramaCreate,
    key: str,
    trace_id: str,
) -> WorkflowRun:
    from arclight.application.short_drama_audio import inspect_dialogue, preflight_dialogue

    request_hash = digest(payload.model_dump(mode="json"))
    with factory() as db:
        episode = require_episode(db, episode_id, actor, owner=True, write=True)
        previous = _existing_short_drama(db, episode_id, actor, key, request_hash)
        if previous is not None:
            return previous
        source = None
        script = db.get(Script, payload.script_version_id)
        if (
            payload.generated_media_rights_confirmed
            and script is not None
            and script.episode_id == episode_id
            and script.status in ("READY_FOR_REVIEW", "APPROVED")
        ):
            _, _, _, source = inspect_dialogue(db, settings, episode.project_id, actor, script)
    # Directory I/O finishes before opening the admission transaction.
    catalog = query_voice_catalog(source) if source is not None else None

    with factory.begin() as db:
        # Same lock order as ordinary Producer admission and accounting.
        lock_gate(db)
        episode = require_episode(db, episode_id, actor, owner=True, write=True)
        db.refresh(episode, with_for_update=True)
        previous = _existing_short_drama(db, episode_id, actor, key, request_hash)
        if previous is not None:
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
        font_id = payload.font_file_id
        font_facts(db, project.id, font_id, required=False)
        if not payload.generated_media_rights_confirmed:
            raise DomainError("SHORT_DRAMA_RIGHTS_REQUIRED", "请确认本次生成素材可用于此短剧", 409)
        from arclight.application.short_drama_visuals import require_image_service

        require_short_drama_text(db, settings, actor)
        require_image_service(
            db, settings, project.id, actor, output_profile(project.output_profile, "FORMAL")
        )
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
        if font_id is None and any(scene.dialogue for scene in content.scenes):
            font_id = bundled_font_receipt(db, settings, project.id).id
        frozen = {
            "script_version_id": str(script.id),
            "script_sha256": digest(script.content_json),
            "request_sha256": request_hash,
            "target_duration_ms": episode.target_duration_ms,
            "output_profile": project.output_profile,
            "font_file_id": str(font_id) if font_id else None,
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
        preflight_dialogue(db, settings, run, project.id, actor, script.id, catalog=catalog)
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

    progress = progress_for(db, run).copy()
    detail = progress.pop(DETAIL, None)
    code = public_short_drama_error(run.error_code)
    if detail and detail["code"] == "ACCOUNT_BALANCE_INSUFFICIENT" and code is None:
        detail = None
    if detail is None and code is not None:
        phase = progress.get("stopped_phase", progress["phase"])
        error = DomainError(code, "制作未完成")
        casting = (
            phase == "AUDIO"
            and code == "ACCOUNT_BALANCE_INSUFFICIENT"
            and any(
                command.snapshot_json["action"] == VOICE_CASTING_ACTION
                for command in pending_commands(db, run, episode.project_id)
            )
        )
        detail = (
            text_failure(
                error,
                run.input_json["created_by"],
                run.input_json.get("_text_service"),
                task=ShortDramaTask(id=run.id, family="WORKFLOW"),
            )
            if casting or phase in {"PLANNING", "ART", "STORYBOARD"}
            else ShortDramaFailure(error, service_kind=None)
        ).record(phase)
    return {
        "run_id": run.id,
        "status": run.status,
        "revision": run.revision,
        "can_retry_balance": run.input_json.get("created_by") == actor
        and balance_blocker(db, run, episode.project_id) is not None,
        "script_version_id": run.input_json["short_drama"]["script_version_id"],
        "error_code": code,
        "issue": public_issue(detail, actor) if detail else None,
        **progress,
    }
