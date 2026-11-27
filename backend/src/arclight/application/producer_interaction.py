"""Durable Producer commands share the original root, permissions and outbox."""

from uuid import UUID, uuid5

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import validate_agent_execution
from arclight.application.agent_runtime.store import submit_agent_decision
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolContext, available_agent_tools
from arclight.application.agent_workflows import creation_loops
from arclight.application.commands import emit, require_run
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate, utc
from arclight.application.producer_diagnostics import public_diagnostic
from arclight.application.producer_planning import producer_registry, start_producer
from arclight.application.production_versions import bind, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.agent_state import terminate_agent_state
from arclight.domain.producer_interaction import (
    ProducerCommand,
    ProducerRunCreate,
    ProducerVoiceCastingCommand,
)
from arclight.domain.producer_planning import DELEGATE_TOOL, DelegationRequest
from arclight.domain.producer_series import (
    PRODUCER_VERSIONS,
    SeriesProducerCreate,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.base import utcnow
from arclight.persistence.models import Episode, OutboxEvent, WorkflowRecoveryCounter, WorkflowRun
from arclight.persistence.versions import VersionBinding

COMMAND = "PRODUCER_COMMAND"
RECEIPT = "PRODUCER_COMMAND_RECEIPT"
RECOVERY = "PRODUCER_RECOVERY_REQUEST"


def require_producer(
    db: Session, run_id: UUID, actor: str, *, active: bool = True
) -> tuple[WorkflowRun, Episode]:
    run = require_run(db, run_id, actor, lock=True)
    validate_publication(db, run.id)
    if run.workflow_type != "producer" or run.workflow_version not in PRODUCER_VERSIONS:
        raise DomainError("NOT_FOUND", "生产协调运行不存在", 404)
    if execution_actor(db, run) != actor:
        raise DomainError("FORBIDDEN", "仅原发起者可操作生产协调运行", 403)
    if active:
        validate_agent_execution(db, run.id, "producer")
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    return run, episode


def command_rows(db: Session, run: WorkflowRun, project_id: UUID) -> list[VersionBinding]:
    rows = db.scalars(
        select(VersionBinding).where(
            VersionBinding.project_id == project_id, VersionBinding.resource_type == COMMAND
        )
    )
    return sorted(
        (row for row in rows if row.snapshot_json.get("run_id") == str(run.id)),
        key=lambda row: row.snapshot_json["revision"],
    )


def command_receipt(db: Session, project_id: UUID, command_id: UUID) -> dict | None:
    row = find_binding(db, project_id, RECEIPT, command_id)
    return row.snapshot_json if row else None


def pending_commands(db: Session, run: WorkflowRun, project_id: UUID) -> list[VersionBinding]:
    return [
        row
        for row in command_rows(db, run, project_id)
        if command_receipt(db, project_id, row.resource_id) is None
    ]


def has_pending_question(db: Session, run_id: UUID) -> bool:
    """An unanswered Producer interrupt pauses new work even after its command returned."""
    for loop in creation_loops(db, run_id, "producer"):
        if loop.status != "WAITING_HUMAN":
            continue
        question_id = (loop.outcome_json or {}).get("question_id")
        if question_id is None or not db.scalar(
            select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == UUID(question_id))
        ):
            return True
    return False


def record_receipt(
    db: Session,
    run: WorkflowRun,
    project_id: UUID,
    command: VersionBinding,
    status: str,
    error_code: str | None = None,
    *,
    issue_detail: dict | None = None,
) -> None:
    """Recording an already executed result needs no new admission or paid action."""
    if command_receipt(db, project_id, command.resource_id) is not None:
        return
    bind(
        db,
        project_id,
        RECEIPT,
        command.resource_id,
        {
            "schema_version": 1,
            "run_id": str(run.id),
            "command_id": str(command.resource_id),
            "revision": command.snapshot_json["revision"],
            "status": status,
            "error_code": error_code,
            **({"issue_detail": issue_detail} if issue_detail is not None else {}),
        },
    )
    emit(
        db,
        run,
        "producer.command.finished",
        {
            "command_id": str(command.resource_id),
            "status": status,
            "error_code": public_diagnostic(error_code),
        },
    )


def _previous(
    db: Session, run: WorkflowRun, project_id: UUID, actor: str, key: str, payload: dict
) -> VersionBinding | None:
    identity = uuid5(run.id, "producer-command:" + key)
    previous = find_binding(db, project_id, COMMAND, identity)
    if previous is not None and (
        previous.snapshot_json["actor_id"] != actor
        or previous.snapshot_json["payload_sha256"] != digest(payload)
    ):
        raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能更换生产命令", 409)
    return previous


def _enqueue(
    db: Session, run: WorkflowRun, episode: Episode, actor: str, key: str, payload: dict
) -> VersionBinding:
    if previous := _previous(db, run, episode.project_id, actor, key, payload):
        return previous
    if pending_commands(db, run, episode.project_id):
        raise DomainError("PRODUCER_COMMAND_PENDING", "请先完成或恢复当前生产命令", 409)
    run.revision += 1
    identity = uuid5(run.id, "producer-command:" + key)
    command = bind(
        db,
        episode.project_id,
        COMMAND,
        identity,
        {
            "schema_version": 1,
            "run_id": str(run.id),
            "actor_id": actor,
            "revision": run.revision,
            "action": payload["action"],
            "payload": payload,
            "payload_sha256": digest(payload),
        },
    )
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(
        db,
        run,
        "producer.command.queued",
        {
            "command_id": str(identity),
            "action": payload["action"],
            "revision": run.revision,
        },
    )
    return command


def start_interactive_producer(
    db: Session,
    settings: Settings,
    episode_id: UUID,
    actor: str,
    payload: ProducerRunCreate,
    key: str,
    request_id: str,
) -> WorkflowRun:
    run = start_producer(
        db,
        settings,
        episode_id,
        actor,
        payload.goal,
        key,
        request_id,
        series=payload.scope if isinstance(payload, SeriesProducerCreate) else None,
    )
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    enqueue_initial_producer_plan(db, run, episode, actor)
    return run


def enqueue_initial_producer_plan(
    db: Session, run: WorkflowRun, episode: Episode, actor: str
) -> None:
    # An HTTP retry after completion must not create another operation.
    if not command_rows(db, run, episode.project_id):
        _enqueue(db, run, episode, actor, "initial-plan", {"action": "PLAN"})


def enqueue_short_drama_voice_assignment(
    db: Session, run: WorkflowRun, episode: Episode, actor: str
) -> None:
    from arclight.application.short_drama_audio import read_voice_casting_input

    casting = read_voice_casting_input(db, episode.project_id, run.id)
    command = ProducerVoiceCastingCommand(casting_sha256=digest(casting.model_dump(mode="json")))
    _enqueue(db, run, episode, actor, "short-drama:assign-voices", command.model_dump(mode="json"))


def validate_delegation(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    episode: Episode,
    actor: str,
    request: DelegationRequest,
) -> None:
    from arclight.application.producer_delegation import (
        require_producer_parent,
        validate_delegation_scope,
    )
    from arclight.application.producer_plan_history import (
        canonical_task,
        find_task_delegation,
        read_plan_record,
    )

    available = available_agent_tools(
        db, producer_registry(settings), run.id, "producer", actor, "producer"
    )
    if DELEGATE_TOOL not in {tool.name for tool in available}:
        raise DomainError("USER_NOT_AUTHORIZED", "生产委派工具当前未获授权", 403)
    context = AgentToolContext(db, run.id, episode.project_id, episode.id, actor, "producer")
    validate_delegation_scope(context, request, settings)
    record = read_plan_record(db, episode.project_id, request.plan_id, actor, require_current=True)
    if find_task_delegation(db, run.id, canonical_task(record, request.task_key)):
        return
    planned = next(task for task in record.plan.tasks if task.task_key == request.task_key)
    from arclight.application.producer_series import (
        require_episode_prerequisites,
        task_context,
    )

    require_episode_prerequisites(
        db, run, record.plan, planned, task_context(run, record.plan, planned.task_key), actor
    )
    for dependency in planned.depends_on:
        scope = request.model_copy(update={"task_key": dependency})
        validate_delegation_scope(context, scope, settings)
        saved = find_task_delegation(db, run.id, canonical_task(record, dependency))
        child = db.get(WorkflowRun, saved.child_run_id) if saved else None
        if child is None or child.status != "SUCCEEDED":
            raise DomainError("PRODUCER_PREREQUISITE_PENDING", "前置生产任务尚未成功", 409)
    require_producer_parent(db, run.id, episode.id, actor)


def queue_command(
    db: Session,
    settings: Settings,
    run_id: UUID,
    actor: str,
    payload: ProducerCommand,
    key: str,
    *,
    automatic: bool = False,
) -> WorkflowRun:
    lock_gate(db)
    specialists = {}
    if payload.action == "CANCEL":
        candidate = db.get(WorkflowRun, run_id)
        if candidate is not None and candidate.input_json.get("short_drama"):
            from arclight.application.short_drama_production import lock_specialists

            specialists = lock_specialists(db, candidate)
    run, episode = require_producer(db, run_id, actor, active=False)
    if (
        run.input_json.get("short_drama")
        and payload.action not in {"CANCEL", "RECOVER"}
        and not automatic
    ):
        raise DomainError("SHORT_DRAMA_AUTOMATIC", "本次短剧由后台自动推进", 409)
    value = payload.model_dump(mode="json")
    if _previous(db, run, episode.project_id, actor, key, value):
        return run
    recovery_id = uuid5(run.id, "producer-recovery:" + key)
    recovery = find_binding(db, episode.project_id, RECOVERY, recovery_id)
    if recovery is not None:
        if recovery.snapshot_json != {"actor_id": actor, "payload": value}:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能更换恢复命令", 409)
        return run
    if run.revision != payload.expected_revision:
        raise DomainError("REVISION_CONFLICT", "生产运行已变化，请刷新后重试", 409)
    if run.status != "RUNNING":
        raise DomainError("AGENT_NOT_ACTIVE", "生产协调运行当前不可操作", 409)
    if payload.action == "CANCEL":
        if run.input_json.get("short_drama"):
            from arclight.application.render_jobs import cancel_job
            from arclight.application.short_drama_production import (
                render_for,
                stop_media,
                stop_specialists,
            )
            from arclight.domain.render_jobs import RenderCancel
            from arclight.persistence.rendering import RenderExecution

            if render := render_for(db, run, episode.project_id):
                execution = db.get(RenderExecution, render.id, populate_existing=True)
                if execution is not None and execution.status == "SUCCEEDED":
                    raise DomainError(
                        "SHORT_DRAMA_ALREADY_RENDERED", "成片已渲染，正在核对交付", 409
                    )
                if execution is not None and execution.status in {"QUEUED", "RUNNING"}:
                    cancel_job(
                        db, render.id, actor, RenderCancel(expected_revision=render.revision)
                    )
            stop_specialists(db, specialists)
            stop_media(db, run, episode.project_id)
        # Cancellation is local and does not require provider admission.
        for pending in pending_commands(db, run, episode.project_id):
            record_receipt(db, run, episode.project_id, pending, "STOPPED", "AGENT_CANCELLED")
        command = _enqueue(db, run, episode, actor, key, value)
        assert run.agent_state_json is not None
        run.agent_state_json = terminate_agent_state(
            run.agent_state_json, scope_for_run(run, episode.project_id), reason="CANCELLED"
        ).model_dump(mode="json")
        run.status, run.error_code = "CANCELLED", None
        record_receipt(db, run, episode.project_id, command, "STOPPED")
        emit(db, run, "workflow.cancelled", {"scope": "producer_root"})
        return run
    validate_agent_execution(db, run.id, "producer")
    if payload.action == "RECOVER":
        outstanding = pending_commands(db, run, episode.project_id)
        original = next((row for row in outstanding if row.resource_id == payload.command_id), None)
        if original is None or original.snapshot_json["revision"] != run.revision:
            raise DomainError("PRODUCER_COMMAND_NOT_PENDING", "原生产命令已完成或不再有效", 409)
        loops = db.scalars(select(AgentLoop).where(AgentLoop.run_id == run.id))
        if any(loop.lease_expires_at and utc(loop.lease_expires_at) > utcnow() for loop in loops):
            raise DomainError("AGENT_LOOP_BUSY", "生产循环仍由原执行器持有", 409)
        outbox = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.run_id == run.id, OutboxEvent.revision == run.revision
            )
        )
        if outbox is None:
            raise DomainError("PRODUCER_COMMAND_INVALID", "原生产命令缺少投递记录", 409)
        if run.error_code == "ACCOUNT_BALANCE_INSUFFICIENT":
            from arclight.application.gateway_accounting import workflow_balance_rejected

            if not workflow_balance_rejected(db, run.id):
                raise DomainError("NOT_RETRYABLE", "原调用须核对供应商回执", 409)
            run.error_code = None
        if run.error_code == "WORKFLOW_RECOVERY_LIMIT":
            run.error_code = None
            db.execute(
                delete(WorkflowRecoveryCounter).where(WorkflowRecoveryCounter.event_id == outbox.id)
            )
        bind(db, episode.project_id, RECOVERY, recovery_id, {"actor_id": actor, "payload": value})
        outbox.published_at = None
        emit(db, run, "producer.command.requeued", {"command_id": str(original.resource_id)})
        return run
    if has_pending_question(db, run.id):
        raise DomainError("PRODUCER_QUESTION_PENDING", "请先回答当前生产问题或取消运行", 409)
    if payload.action == "DELEGATE":
        validate_delegation(
            db,
            settings,
            run,
            episode,
            actor,
            DelegationRequest.model_validate(
                payload.model_dump(exclude={"action", "expected_revision"})
            ),
        )
    else:
        from arclight.application.producer_plan_history import read_current_plan

        current = read_current_plan(db, run, actor)
        if (
            current.plan_id != payload.observation.base_plan_id
            or current.plan_sha256 != payload.observation.base_plan_sha256
        ):
            raise DomainError("PRODUCER_PLAN_SUPERSEDED", "重规划必须基于当前精确计划", 409)
    _enqueue(db, run, episode, actor, key, value)
    return run


def queue_answer(
    db: Session,
    run_id: UUID,
    loop_id: UUID,
    actor: str,
    decision: AgentHumanDecision,
) -> WorkflowRun:
    lock_gate(db)
    run, episode = require_producer(db, run_id, actor, active=False)
    if run.input_json.get("short_drama"):
        raise DomainError("SHORT_DRAMA_AUTOMATIC", "本次短剧由后台自动推进", 409)
    loop = db.get(AgentLoop, loop_id)
    if (
        loop is None
        or loop.run_id != run.id
        or loop.actor_id != actor
        or loop.seed_json.get("agent_name") != "producer"
    ):
        raise DomainError("NOT_FOUND", "当前生产问题不存在", 404)
    payload = {
        "action": "ANSWER",
        "loop_id": str(loop_id),
        "decision": decision.model_dump(mode="json"),
    }
    key = "answer:" + str(decision.question_id)
    if _previous(db, run, episode.project_id, actor, key, payload):
        return run
    validate_agent_execution(db, run.id, "producer")
    if pending_commands(db, run, episode.project_id):
        raise DomainError("PRODUCER_COMMAND_PENDING", "请先恢复当前生产命令", 409)
    submit_agent_decision(db, loop_id, actor, decision)
    _enqueue(db, run, episode, actor, key, payload)
    return run
