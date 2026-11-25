"""Agent execution scope, action identities and usage reporting."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import require_project
from arclight.application.gateway_accounting import latest_receipt, media_outcome
from arclight.application.gateway_text import execution_actor
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext
from arclight.domain.agent_state import AgentStateError, state_for_scope
from arclight.domain.agent_tools import AgentName
from arclight.domain.model_gateway import GatewayRequest
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    Episode,
    User,
    WorkflowRun,
)


def _enabled_actor(db: Session, actor: str) -> None:
    if actor == "service-owner":
        return
    try:
        identity = UUID(actor)
    except ValueError:
        raise DomainError("FORBIDDEN", "Agent 调用者无效", 403) from None
    user = db.get(User, identity, populate_existing=True)
    if user is None or user.disabled:
        raise DomainError("FORBIDDEN", "Agent 调用者不可执行", 403)


def _active_chain(
    db: Session,
    control: AgentRunLimit,
    stage: str | None = None,
    *,
    successor_run: WorkflowRun | None = None,
) -> None:

    root_id, project_id, actor = control.root_run_id, control.project_id, control.actor_id
    _enabled_actor(db, actor)
    require_project(db, project_id, actor, write=True)
    visited: set[UUID] = set()
    current = control
    child_run = successor_run
    root = db.get(AgentRunLimit, root_id)
    if root is None:
        raise DomainError("AGENT_LIMITS_INVALID", "Agent 根运行限制缺失", 409)
    while True:
        if current.run_id in visited:
            raise DomainError("AGENT_LIMITS_INVALID", "Agent 委派链无效", 409)
        visited.add(current.run_id)
        run = db.get(WorkflowRun, current.run_id, populate_existing=True)
        episode = db.get(Episode, run.episode_id, populate_existing=True) if run else None
        if (
            run is None
            or episode is None
            or episode.project_id != project_id
            or current.root_run_id != root_id
            or current.project_id != project_id
            or current.actor_id != actor
            or execution_actor(db, run) != actor
        ):
            raise DomainError("AGENT_SCOPE_INVALID", "Agent 委派作用范围不匹配", 409)
        if run.status == "CANCELLED":
            raise DomainError("AGENT_CANCELLED", "Agent 运行已取消", 409)
        from arclight.application.producer_series_delivery import paused

        if paused(db, run):
            raise DomainError("PRODUCER_PAUSED", "多集制片已暂停，恢复前不允许新的执行动作", 409)
        completed_review = False
        if child_run is not None and run.status in {"SUCCEEDED", "REJECTED"}:
            from arclight.application.narrative_review_human import completed_review_parent

            completed_review = completed_review_parent(db, run, child_run)
        if (run.status != "RUNNING" and not completed_review) or episode.status == "ARCHIVED":
            raise DomainError("AGENT_NOT_ACTIVE", "Agent 或其父运行当前不可执行", 409)
        scope = scope_for_run(run, project_id)
        if scope.input_sha256 != current.input_sha256:
            raise DomainError("AGENT_INPUT_CHANGED", "Agent 冻结输入已变化", 409)
        if run.agent_state_json is None:
            raise DomainError("RUN_STATE_INVALID", "Agent 运行状态缺失", 409)
        try:
            state = state_for_scope(run.agent_state_json, scope)
        except AgentStateError:
            raise DomainError("RUN_STATE_INVALID", "Agent 运行状态无效", 409) from None
        active = [step.step_id for step in state.steps if step.status == "RUNNING"]
        if completed_review and state.status != run.status:
            raise DomainError("AGENT_NOT_ACTIVE", "已结束叙事父审查的运行状态不一致", 409)
        if not completed_review and (
            state.status != "RUNNING"
            or len(active) != 1
            or (current.run_id == control.run_id and stage is not None and active != [stage])
        ):
            raise DomainError("AGENT_NOT_ACTIVE", "Agent 请求不属于活动步骤", 409)
        if current.parent_run_id is None:
            if current.run_id != root_id or current.depth != 0:
                raise DomainError("AGENT_LIMITS_INVALID", "Agent 根运行记录无效", 409)
            return
        parent = db.get(AgentRunLimit, current.parent_run_id)
        if parent is None or parent.depth + 1 != current.depth:
            raise DomainError("AGENT_LIMITS_INVALID", "Agent 父运行记录无效", 409)
        child_run, current = run, parent


def validate_agent_execution(db: Session, run_id: UUID, stage: str) -> None:
    """Recheck the active scope without reserving another action or charging completion."""
    control = db.get(AgentRunLimit, run_id)
    if control is None:
        raise DomainError("AGENT_LIMITS_REQUIRED", "Agent 调用尚未建立运行限制", 409)
    _active_chain(db, control, stage)


def delegation_count(db: Session, root_run_id: UUID) -> int:
    """Count physical children and committed logical specialist delegations together."""
    from arclight.domain.director_collaboration import REQUEST_OPINION

    runs = list(
        db.scalars(select(AgentRunLimit.run_id).where(AgentRunLimit.root_run_id == root_run_id))
    )
    receipts = db.scalars(
        select(AgentToolInvocation).where(
            AgentToolInvocation.run_id.in_(runs),
            AgentToolInvocation.tool_name == REQUEST_OPINION,
            AgentToolInvocation.status == "SUCCEEDED",
        )
    )
    logical = {(row.feedback_json or {})["result"]["delegation_id"] for row in receipts}
    return max(0, len(runs) - 1) + len(logical)


def configure_agent_run(
    db: Session,
    run_id: UUID,
    actor: str,
    *,
    parent_run_id: UUID | None = None,
) -> AgentRunLimit:
    """Bind the actor and run lineage before the first action."""
    from arclight.application.model_gateway import lock_gate

    lock_gate(db)
    _enabled_actor(db, actor)
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    episode = db.get(Episode, run.episode_id, populate_existing=True) if run else None
    if run is None or episode is None:
        raise DomainError("NOT_FOUND", "Agent 运行不存在", 404)
    require_project(db, episode.project_id, actor, write=True)
    if execution_actor(db, run) != actor:
        raise DomainError("FORBIDDEN", "Agent 限制须绑定运行发起者", 403)
    parent = db.get(AgentRunLimit, parent_run_id) if parent_run_id else None
    if parent_run_id is not None and (
        parent is None or parent.actor_id != actor or parent.project_id != episode.project_id
    ):
        raise DomainError("AGENT_SCOPE_INVALID", "委派父运行不属于当前作用范围", 409)
    scope = scope_for_run(run, episode.project_id)
    previous = db.get(AgentRunLimit, run.id)
    if previous is not None:
        if (
            previous.parent_run_id != parent_run_id
            or previous.actor_id != actor
            or previous.project_id != episode.project_id
            or previous.input_sha256 != scope.input_sha256
        ):
            raise DomainError("AGENT_LIMITS_CONFLICT", "运行限制不可重新配置或更换父运行", 409)
        return previous
    if run.status not in {"QUEUED", "RUNNING"} or episode.status == "ARCHIVED":
        raise DomainError("AGENT_NOT_ACTIVE", "只能为活动运行建立限制", 409)
    if db.scalar(select(GatewayCall.id).where(GatewayCall.workflow_id == run.id).limit(1)) or (
        db.scalar(
            select(AgentToolInvocation.id).where(AgentToolInvocation.run_id == run.id).limit(1)
        )
    ):
        raise DomainError("AGENT_LIMITS_LATE", "已有调用的运行不能补建限制", 409)
    root_id, depth = run.id, 0
    if parent is not None:
        _active_chain(db, parent, successor_run=run)
        root_id, depth = parent.root_run_id, parent.depth + 1
    row = AgentRunLimit(
        run_id=run.id,
        project_id=episode.project_id,
        root_run_id=root_id,
        parent_run_id=parent_run_id,
        actor_id=actor,
        input_sha256=scope.input_sha256,
        depth=depth,
    )
    db.add(row)
    db.flush()
    return row


def action_usage(db: Session, root_run_id: UUID) -> dict:
    """Internal projection of authoritative receipts; no second monetary ledger."""
    actions = list(db.scalars(select(AgentAction).where(AgentAction.root_run_id == root_run_id)))
    actual = pending = Decimal("0")
    represented_media_jobs: set[UUID] = set()
    unknown_cost = False
    for action in actions:
        if action.gateway_call_id is None:
            continue
        call = db.get(GatewayCall, action.gateway_call_id)
        assert call is not None
        outcome = media_outcome(db, call) if call.generation_attempt_id else None
        if call.generation_attempt_id:
            from arclight.persistence.generation import GenerationAttempt

            attempt = db.get(GenerationAttempt, call.generation_attempt_id)
            if attempt:
                represented_media_jobs.add(attempt.job_id)
        receipt = latest_receipt(db, call.id)
        cost = (
            outcome["usage"]["actual_cost"]
            if outcome is not None
            else receipt.actual_cost
            if receipt is not None
            else None
        )
        if cost is not None:
            actual += Decimal(cost)
        elif call.maximum_cost is not None:
            pending += call.maximum_cost
        else:
            unknown_cost = True
    from arclight.application.art_concept_jobs import concept_costs

    concept_actual, concept_pending = concept_costs(
        db, root_run_id, exclude_jobs=represented_media_jobs
    )
    actual += concept_actual
    pending += concept_pending
    return {
        "actions": actions,
        "actual_cost": actual,
        "pending_estimate": pending,
        "unknown_cost": unknown_cost,
    }


def _check_action(db: Session, context: AgentExecutionContext) -> AgentRunLimit:
    control = db.get(AgentRunLimit, context.run_id)
    if control is None:
        raise DomainError("AGENT_SCOPE_REQUIRED", "Agent 运行未绑定执行范围", 409)
    _active_chain(db, control, context.stage)
    return control


def _gateway_context(db: Session, request: GatewayRequest) -> AgentExecutionContext | None:
    context = request.execution
    if context is None:
        if (
            request.workflow_id is not None
            and db.get(AgentRunLimit, request.workflow_id) is not None
        ):
            raise DomainError("AGENT_CONTEXT_REQUIRED", "受限运行须提供执行器行动标识", 409)
        return None
    if request.workflow_id is not None and request.workflow_id != context.run_id:
        raise DomainError("AGENT_SCOPE_INVALID", "模型调用与 Agent 运行不匹配", 409)
    if request.workflow_id is None and (
        request.generation_attempt_id is None or context.kind != "TOOL"
    ):
        raise DomainError("AGENT_SCOPE_INVALID", "Agent 调用缺少权威运行来源", 409)
    return context


def check_gateway_action(db: Session, request: GatewayRequest) -> None:
    """Called under the existing gateway gate, before its immutable reservation is written."""
    context = _gateway_context(db, request)
    # A safely unaccepted attempt may be retried, but its executor identity is frozen.
    # A media retry must retain its original Agent identity.
    previous = db.execute(
        select(GatewayCall, AgentAction)
        .outerjoin(AgentAction, AgentAction.gateway_call_id == GatewayCall.id)
        .where(
            GatewayCall.project_id == request.project_id,
            GatewayCall.operation_key == request.operation_key,
        )
    )
    for _, action in previous:
        identity = (
            (
                action.run_id,
                action.agent_name,
                action.stage,
                action.step_key,
                action.kind,
                action.tool_name,
            )
            if action is not None
            else None
        )
        requested = (
            (
                context.run_id,
                context.agent_name,
                context.stage,
                context.step_key,
                context.kind,
                context.tool_name,
            )
            if context is not None
            else None
        )
        if identity != requested:
            raise DomainError("GATEWAY_IDEMPOTENCY_CONFLICT", "重试不能更换执行器行动标识", 409)
    if context is None:
        return
    control = _check_action(db, context)
    if control.project_id != request.project_id or control.actor_id != request.actor:
        raise DomainError("AGENT_SCOPE_INVALID", "Agent 调用者或项目不匹配", 409)
    if context.kind == "MODEL" and request.task != context.stage:
        raise DomainError("AGENT_SCOPE_INVALID", "模型调用不属于当前步骤", 409)
    _check_paid_tool_grant(db, context, request)


def _check_paid_tool_grant(
    db: Session, context: AgentExecutionContext, request: GatewayRequest
) -> None:
    if context.kind == "TOOL":
        grant = db.scalar(
            select(AgentToolGrant).where(
                AgentToolGrant.run_id == context.run_id,
                AgentToolGrant.agent_name == context.agent_name,
            )
        )
        if (
            grant is None
            or grant.actor_id != request.actor
            or grant.project_id != request.project_id
            or context.tool_name not in grant.allowed_tools_json
        ):
            raise DomainError("AGENT_TOOL_GRANT_FORBIDDEN", "收费工具未获当前用户授权", 403)


def record_gateway_action(db: Session, request: GatewayRequest, call_id: UUID) -> None:
    if request.execution is not None:
        _record_action(db, request.execution, gateway_call_id=call_id)


def validate_gateway_dispatch(db: Session, request: GatewayRequest) -> None:
    """Recheck live cancellation, scope and paid tool authorization before I/O."""
    context = _gateway_context(db, request)
    if context is not None:
        _check_action(db, context)
        _check_paid_tool_grant(db, context, request)


def check_tool_action(
    db: Session,
    run_id: UUID,
    agent_name: AgentName,
    stage: str,
    tool_name: str,
    actor: str,
    step_key: str,
) -> AgentExecutionContext | None:
    if db.get(AgentRunLimit, run_id) is None:
        return None
    context = AgentExecutionContext.model_validate(
        dict(
            run_id=run_id,
            agent_name=agent_name,
            stage=stage,
            step_key=step_key,
            kind="TOOL",
            tool_name=tool_name,
        )
    )
    control = _check_action(db, context)
    if control.actor_id != actor:
        raise DomainError("AGENT_SCOPE_INVALID", "工具授权者与 Agent 发起者不匹配", 403)
    return context


def _record_action(
    db: Session,
    context: AgentExecutionContext,
    *,
    gateway_call_id: UUID | None = None,
    tool_invocation_id: UUID | None = None,
) -> None:
    control = db.get(AgentRunLimit, context.run_id)
    assert control is not None
    db.add(
        AgentAction(
            run_id=context.run_id,
            root_run_id=control.root_run_id,
            agent_name=context.agent_name,
            stage=context.stage,
            step_key=context.step_key,
            kind=context.kind,
            tool_name=context.tool_name,
            gateway_call_id=gateway_call_id,
            tool_invocation_id=tool_invocation_id,
        )
    )
    db.flush()


def record_tool_action(
    db: Session, context: AgentExecutionContext | None, invocation_id: UUID
) -> None:
    if context is not None:
        _record_action(db, context, tool_invocation_id=invocation_id)
