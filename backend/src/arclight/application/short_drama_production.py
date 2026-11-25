"""Advance durable short-drama production from the existing worker scheduler."""

import logging
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import cancel_workflow_state, emit, retry_run
from arclight.application.gateway_accounting import balance_rejected, workflow_balance_rejected
from arclight.application.generation_attempts import request_retry
from arclight.application.generation_scheduling import latest_media_receipt
from arclight.application.generation_state import request_cancel
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_interaction import (
    command_receipt,
    command_rows,
    pending_commands,
    queue_command,
    record_receipt,
    require_producer,
)
from arclight.application.producer_plan_history import (
    canonical_task,
    find_task_delegation,
    read_current_plan,
)
from arclight.application.production_versions import bind, find_binding
from arclight.application.render_reports import delivery_acceptance
from arclight.application.short_drama import (
    PROGRESS_EVENT,
    progress_for,
    public_short_drama_error,
    require_exact_inputs,
)
from arclight.application.short_drama_budget import MEDIA_SCOPE
from arclight.application.short_drama_delivery import (
    queue_short_drama_delivery,
    render_key,
    require_run_authorization,
)
from arclight.application.short_drama_reviews import apply_art_selection, approve_board
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_state import terminate_agent_state, transition_agent_step
from arclight.domain.producer_interaction import ProducerDelegateCommand, ProducerRecoverCommand
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.models import Episode, GenerationJob, Storyboard, WorkflowRun
from arclight.persistence.render_jobs import RenderJob
from arclight.persistence.render_manifests import RenderArtifact
from arclight.persistence.rendering import RenderExecution
from arclight.persistence.versions import VersionBinding

logger = logging.getLogger(__name__)
ACTIVE = {"QUEUED", "RUNNING", "WAITING_REVIEW"}


def set_progress(db: Session, run: WorkflowRun, phase: str, **facts) -> None:
    value = {"phase": phase, **facts}
    if progress_for(db, run) != value:
        emit(db, run, PROGRESS_EVENT, value)


def lock_specialists(db: Session, run: WorkflowRun) -> dict[UUID, WorkflowRun]:
    # Reviews and worker delivery take their workflow lock before the project lock.
    return {
        child.id: child
        for child in db.scalars(
            select(WorkflowRun)
            .join(AgentRunLimit, AgentRunLimit.run_id == WorkflowRun.id)
            .where(AgentRunLimit.parent_run_id == run.id, AgentRunLimit.root_run_id == run.id)
            .order_by(WorkflowRun.id)
            .with_for_update(of=WorkflowRun)
            .execution_options(populate_existing=True)
        )
    }


def stop_specialists(db: Session, specialists: dict[UUID, WorkflowRun]) -> None:
    for child in specialists.values():
        if child.status in ACTIVE:
            cancel_workflow_state(db, child)


def stop_media(db: Session, run: WorkflowRun, project_id: UUID) -> None:
    jobs = db.scalars(
        select(GenerationJob)
        .join(VersionBinding, VersionBinding.resource_id == GenerationJob.id)
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == MEDIA_SCOPE,
            VersionBinding.snapshot_json["run_id"].as_string() == str(run.id),
            GenerationJob.project_id == project_id,
            GenerationJob.status.not_in({"SUCCEEDED", "FAILED", "CANCELLED"}),
            GenerationJob.cancel_requested_at.is_(None),
        )
        .order_by(GenerationJob.id)
        .execution_options(populate_existing=True)
    )
    for job in jobs:
        request_cancel(db, job.id, job.revision, job.created_by)


def render_for(db: Session, run: WorkflowRun, project_id: UUID) -> RenderJob | None:
    return db.scalar(
        select(RenderJob).where(
            RenderJob.project_id == project_id,
            RenderJob.creation_key == render_key(run.id),
        )
    )


def finish_render(db: Session, run: WorkflowRun, project_id: UUID, render: RenderJob) -> None:
    execution = db.get(RenderExecution, render.id, populate_existing=True)
    if execution is None:
        raise DomainError("SHORT_DRAMA_RENDER_MISSING", "找不到本次渲染记录", 409)
    progress = progress_for(db, run)
    facts = {"storyboard_id": progress.get("storyboard_id"), "render_job_id": str(render.id)}
    if execution.status in {"FAILED", "CANCELLED"}:
        raise DomainError(
            execution.error_code or "SHORT_DRAMA_RENDER_FAILED", "成片渲染未完成", 409
        )
    if execution.status != "SUCCEEDED":
        set_progress(db, run, "RENDERING", **facts, render_progress=render.progress)
        return
    artifact = db.scalar(select(RenderArtifact).where(RenderArtifact.job_id == render.id))
    if (
        artifact is None
        or artifact.file_id != execution.output_file_id
        or artifact.manifest_id != render.manifest_id
    ):
        raise DomainError("SHORT_DRAMA_RENDER_MISSING", "成片缺少可验证的交付文件", 409)
    if delivery_acceptance(db, artifact, run.input_json["created_by"]).phase != "COMPLETED":
        raise DomainError("SHORT_DRAMA_DELIVERY_QA", "成片未通过自动交付检查", 409)
    assert run.agent_state_json is not None
    run.agent_state_json = transition_agent_step(
        run.agent_state_json,
        scope_for_run(run, project_id),
        expected_revision=run.agent_state_json["revision"],
        step_id="producer",
        target="SUCCEEDED",
    ).model_dump(mode="json")
    run.status = "SUCCEEDED"
    run.revision += 1
    set_progress(db, run, "DELIVERED", **facts, delivery_id=str(artifact.id), render_progress=100)
    emit(db, run, "workflow.finished", {"status": run.status, "delivery_id": str(artifact.id)})


def advance(
    db: Session, settings: Settings, run: WorkflowRun, specialists: dict[UUID, WorkflowRun]
) -> None:
    episode, actor, spec = require_run_authorization(db, run)
    project_id = episode.project_id
    script = require_exact_inputs(db, episode, spec)
    if render := render_for(db, run, project_id):
        finish_render(db, run, project_id, render)
        return
    control = db.get(AgentRunLimit, run.id)
    if control is None:
        raise DomainError("SHORT_DRAMA_INACTIVE", "本次短剧制作缺少执行范围", 409)
    commands = command_rows(db, run, project_id)
    if not commands:
        raise DomainError("SHORT_DRAMA_COMMAND_MISSING", "本次制作缺少原制片命令", 409)
    for command in commands:
        completed = command_receipt(db, project_id, command.resource_id)
        if completed is None:
            if db.scalar(
                select(AgentLoop.id)
                .where(AgentLoop.run_id == run.id, AgentLoop.status == "RECOVERY_REQUIRED")
                .limit(1)
            ):
                set_progress(db, run, "RECONCILING")
            return
        if completed["status"] != "COMMITTED":
            raise DomainError(
                completed.get("error_code") or "SHORT_DRAMA_PLAN_BLOCKED",
                "制片规划或专业任务启动未通过制作检查",
            )
    plan = read_current_plan(db, run, actor)
    director = None
    for planned in plan.plan.tasks:
        phase = "ART" if planned.task.kind == "art_director" else "STORYBOARD"
        delegation = find_task_delegation(db, run.id, canonical_task(plan, planned.task_key))
        if delegation is None:
            set_progress(db, run, phase)
            queue_command(
                db,
                settings,
                run.id,
                actor,
                ProducerDelegateCommand(
                    action="DELEGATE",
                    expected_revision=run.revision,
                    plan_id=plan.plan_id,
                    plan_sha256=plan.plan_sha256,
                    task_key=planned.task_key,
                ),
                f"short-drama:{planned.task_key}",
                automatic=True,
            )
            return
        child = specialists.get(delegation.child_run_id)
        if child is None or child.episode_id != episode.id:
            raise DomainError("SHORT_DRAMA_CHILD_MISSING", "找不到本次制作的专业任务", 409)
        if child.status == "WAITING_REVIEW":
            ready = (
                apply_art_selection(db, run, child, project_id)
                if phase == "ART"
                else approve_board(db, settings, run, child, project_id)
            )
            if ready:
                if phase == "STORYBOARD":
                    assert child.resume_json is not None
                    set_progress(db, run, phase, storyboard_id=child.resume_json["version_id"])
                return
        if child.status in ACTIVE:
            if db.scalar(
                select(AgentLoop.id)
                .where(
                    AgentLoop.run_id == child.id,
                    AgentLoop.status == "WAITING_HUMAN",
                )
                .limit(1)
            ):
                raise DomainError(
                    "SHORT_DRAMA_SPECIALIST_BLOCKED", "专业任务缺少继续制作所需的输入", 409
                )
            return
        if child.status != "SUCCEEDED":
            raise DomainError(
                child.error_code or "SHORT_DRAMA_SPECIALIST_FAILED", "专业任务未成功完成", 409
            )
        if phase == "STORYBOARD":
            director = child
    if director is None:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "缺少本次制作的精确分镜", 409)
    board = db.scalar(
        select(Storyboard).where(
            Storyboard.run_id == director.id,
            Storyboard.script_version_id == script.id,
            Storyboard.status == "APPROVED",
        )
    )
    if board is None:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "找不到本次制作的已检查分镜", 409)

    from arclight.application.short_drama_audio import advance_short_drama_audio

    facts = {"storyboard_id": str(board.id)}
    sound = advance_short_drama_audio(db, settings, run, board.id)
    if sound is None:
        set_progress(db, run, "AUDIO", **facts)
        return

    from arclight.application.short_drama_visuals import advance_short_drama_visuals

    shots = advance_short_drama_visuals(
        db,
        settings,
        run,
        board.id,
    )
    if shots is None:
        set_progress(db, run, "SHOTS", **facts)
        return
    render_result = queue_short_drama_delivery(
        db,
        settings,
        run,
        board.id,
        shots,
        voice_asset_versions=sound.voice_asset_versions,
        sound_clips=sound.sound_clips,
    )
    set_progress(
        db, run, "RENDERING", **facts, render_job_id=str(render_result.id), render_progress=0
    )


def balance_blocker(
    db: Session, run: WorkflowRun, project_id: UUID
) -> WorkflowRun | GenerationJob | None:
    if run.status != "RUNNING" or run.error_code != "ACCOUNT_BALANCE_INSUFFICIENT":
        return None
    if pending_commands(db, run, project_id) and workflow_balance_rejected(db, run.id):
        return run
    for child in db.scalars(
        select(WorkflowRun)
        .join(AgentRunLimit, AgentRunLimit.run_id == WorkflowRun.id)
        .where(
            AgentRunLimit.root_run_id == run.id,
            WorkflowRun.id != run.id,
            WorkflowRun.status == "FAILED",
            WorkflowRun.error_code == "ACCOUNT_BALANCE_INSUFFICIENT",
        )
    ):
        if workflow_balance_rejected(db, child.id):
            return child
    for job in db.scalars(
        select(GenerationJob)
        .join(VersionBinding, VersionBinding.resource_id == GenerationJob.id)
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == MEDIA_SCOPE,
            VersionBinding.snapshot_json["run_id"].as_string() == str(run.id),
            GenerationJob.status == "FAILED",
            GenerationJob.cancel_requested_at.is_(None),
        )
    ):
        if balance_rejected(latest_media_receipt(db, job)):
            return job
    return None


def retry_short_drama(
    db: Session, settings: Settings, run_id: UUID, revision: int, actor: str, key: str
) -> WorkflowRun:
    lock_gate(db)
    run, episode = require_producer(db, run_id, actor)
    _, _, spec = require_run_authorization(db, run)
    require_exact_inputs(db, episode, spec)
    receipt_id = uuid5(run.id, "short-drama-balance-retry:" + key)
    intent = {"run_id": str(run.id), "revision": revision, "actor": actor}
    kind = "SHORT_DRAMA_BALANCE_RETRY"
    if prior := find_binding(db, episode.project_id, kind, receipt_id):
        if prior.snapshot_json != intent:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能更换恢复请求", 409)
        return run
    if run.revision != revision:
        raise DomainError("REVISION_CONFLICT", "制作进度已变化，请刷新后重试", 409)
    target = balance_blocker(db, run, episode.project_id)
    if target is None:
        raise DomainError("NOT_RETRYABLE", "当前没有可继续的余额不足请求", 409)
    if target.id == run.id:
        command = pending_commands(db, run, episode.project_id)[-1]
        queue_command(
            db,
            settings,
            run.id,
            actor,
            ProducerRecoverCommand(
                action="RECOVER", expected_revision=revision, command_id=command.resource_id
            ),
            key,
        )
    elif isinstance(target, GenerationJob):
        request_retry(db, target.id, target.revision, actor)
    else:
        child = db.get(WorkflowRun, target.id, populate_existing=True, with_for_update=True)
        assert child is not None
        retry_run(db, child, automatic_short_drama=True)
    run.error_code = None
    bind(db, episode.project_id, kind, receipt_id, intent)
    return run


def advance_pending(factory: sessionmaker[Session], settings: Settings) -> None:
    with factory() as db:
        identities = list(
            db.scalars(
                select(WorkflowRun.id)
                .where(
                    WorkflowRun.workflow_type == "producer",
                    WorkflowRun.status == "RUNNING",
                    WorkflowRun.error_code.is_distinct_from("ACCOUNT_BALANCE_INSUFFICIENT"),
                    WorkflowRun.error_code.is_distinct_from("WORKFLOW_RECOVERY_LIMIT"),
                    WorkflowRun.input_json["short_drama"]["script_version_id"]
                    .as_string()
                    .is_not(None),
                )
                .order_by(WorkflowRun.updated_at, WorkflowRun.id)
                .limit(12)
            )
        )
    for identity in identities:
        try:
            with factory.begin() as db:
                lock_gate(db)
                run = db.get(WorkflowRun, identity, populate_existing=True, with_for_update=True)
                if (
                    run is None
                    or run.status != "RUNNING"
                    or run.error_code in {"ACCOUNT_BALANCE_INSUFFICIENT", "WORKFLOW_RECOVERY_LIMIT"}
                ):
                    continue
                specialists = lock_specialists(db, run)
                try:
                    with db.begin_nested():
                        advance(db, settings, run, specialists)
                except DomainError as exc:
                    if exc.code == "ACCOUNT_BALANCE_INSUFFICIENT":
                        run.error_code = exc.code
                        continue
                    if exc.status_code >= 500:
                        raise
                    episode = db.get(Episode, run.episode_id)
                    assert episode is not None and run.agent_state_json is not None
                    stop_specialists(db, specialists)
                    stop_media(db, run, episode.project_id)
                    for command in pending_commands(db, run, episode.project_id):
                        record_receipt(db, run, episode.project_id, command, "STOPPED", exc.code)
                    run.agent_state_json = terminate_agent_state(
                        run.agent_state_json,
                        scope_for_run(run, episode.project_id),
                        reason="EXECUTION_FAILED",
                    ).model_dump(mode="json")
                    run.status, run.error_code = "FAILED", exc.code
                    run.revision += 1
                    previous = progress_for(db, run)
                    set_progress(
                        db,
                        run,
                        "FAILED",
                        stopped_phase=previous["phase"],
                        **{key: value for key, value in previous.items() if key != "phase"},
                    )
                    emit(db, run, "workflow.failed", {"code": public_short_drama_error(exc.code)})
                run.updated_at = utcnow()
        except Exception:
            logger.exception("short_drama.progression_failed", extra={"run_id": str(identity)})
