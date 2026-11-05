"""Atomic Agent admission; billing remains in the shared gateway and GEN ledgers."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import require_project
from arclight.application.gateway_accounting import latest_receipt, media_outcome
from arclight.application.gateway_text import execution_actor
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext, AgentRunLimits
from arclight.domain.agent_state import AgentStateError, state_for_scope
from arclight.domain.agent_tools import AgentName
from arclight.domain.model_gateway import GatewayRequest, GatewayRoute
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.base import utcnow
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    Episode,
    User,
    WorkflowRun,
)


def _policy(control: AgentRunLimit) -> AgentRunLimits:
    try:
        return AgentRunLimits.model_validate(control.limits_json)
    except ValidationError:
        raise DomainError("AGENT_LIMITS_INVALID", "Agent 限制记录无效", 409) from None


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


def _active_chain(db: Session, control: AgentRunLimit, stage: str | None = None) -> AgentRunLimits:
    from arclight.application.model_gateway import utc

    root_id, project_id, actor = control.root_run_id, control.project_id, control.actor_id
    _enabled_actor(db, actor)
    require_project(db, project_id, actor, write=True)
    visited: set[UUID] = set()
    current = control
    root = db.get(AgentRunLimit, root_id)
    if root is None:
        raise DomainError("AGENT_LIMITS_INVALID", "Agent 根运行限制缺失", 409)
    limits = _policy(root)
    while True:
        if current.run_id in visited or len(visited) > 8:
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
            or current.limits_json != root.limits_json
            or utc(current.deadline_at) != utc(root.deadline_at)
        ):
            raise DomainError("AGENT_SCOPE_INVALID", "Agent 委派作用范围不匹配", 409)
        if run.status == "CANCELLED":
            raise DomainError("AGENT_CANCELLED", "Agent 运行已取消", 409)
        if utcnow() >= utc(root.deadline_at):
            raise DomainError("AGENT_DEADLINE_EXCEEDED", "Agent 已达到总执行时限", 409)
        if run.status != "RUNNING" or episode.status == "ARCHIVED":
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
        if (
            state.status != "RUNNING"
            or len(active) != 1
            or (current.run_id == control.run_id and stage is not None and active != [stage])
        ):
            raise DomainError("AGENT_NOT_ACTIVE", "Agent 请求不属于活动步骤", 409)
        if current.parent_run_id is None:
            if current.run_id != root_id or current.depth != 0:
                raise DomainError("AGENT_LIMITS_INVALID", "Agent 根运行记录无效", 409)
            return limits
        parent = db.get(AgentRunLimit, current.parent_run_id)
        if parent is None or parent.depth + 1 != current.depth:
            raise DomainError("AGENT_LIMITS_INVALID", "Agent 父运行记录无效", 409)
        current = parent


def validate_agent_execution(db: Session, run_id: UUID, stage: str) -> AgentRunLimits:
    """Recheck the active scope without reserving another action or charging completion."""
    control = db.get(AgentRunLimit, run_id)
    if control is None:
        raise DomainError("AGENT_LIMITS_REQUIRED", "Agent 调用尚未建立运行限制", 409)
    return _active_chain(db, control, stage)


def configure_agent_run(
    db: Session,
    run_id: UUID,
    actor: str,
    limits: AgentRunLimits | None = None,
    *,
    parent_run_id: UUID | None = None,
) -> AgentRunLimit:
    """Freeze a policy before the first action; delegated runs inherit the root policy."""
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
    if parent_run_id is not None:
        if parent is None or parent.actor_id != actor or parent.project_id != episode.project_id:
            raise DomainError("AGENT_SCOPE_INVALID", "委派父运行不属于当前作用范围", 409)
        inherited = _policy(parent)
        if limits is not None and limits != inherited:
            raise DomainError("AGENT_LIMITS_CONFLICT", "委派不得更换根运行限制", 409)
        limits = inherited
    limits = AgentRunLimits.model_validate((limits or AgentRunLimits()).model_dump(mode="json"))
    scope = scope_for_run(run, episode.project_id)
    previous = db.get(AgentRunLimit, run.id)
    if previous is not None:
        if (
            previous.parent_run_id != parent_run_id
            or previous.actor_id != actor
            or previous.project_id != episode.project_id
            or previous.input_sha256 != scope.input_sha256
            or _policy(previous) != limits
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
    root_id, depth, deadline = run.id, 0, utcnow() + timedelta(seconds=limits.max_duration_seconds)
    if parent is not None:
        _active_chain(db, parent)
        root_id, depth, deadline = parent.root_run_id, parent.depth + 1, parent.deadline_at
        delegates = list(
            db.scalars(select(AgentRunLimit).where(AgentRunLimit.root_run_id == root_id))
        )
        if depth > limits.max_delegation_depth:
            raise DomainError("AGENT_DELEGATION_DEPTH", "Agent 委派深度已达上限", 409)
        if len(delegates) - 1 >= limits.max_delegations:
            raise DomainError("AGENT_DELEGATION_LIMIT", "Agent 委派次数已达上限", 409)
    row = AgentRunLimit(
        run_id=run.id,
        project_id=episode.project_id,
        root_run_id=root_id,
        parent_run_id=parent_run_id,
        actor_id=actor,
        input_sha256=scope.input_sha256,
        depth=depth,
        limits_json=limits.model_dump(mode="json"),
        deadline_at=deadline,
    )
    db.add(row)
    db.flush()
    return row


def action_usage(db: Session, root_run_id: UUID) -> dict:
    """Internal projection of authoritative receipts; no second monetary ledger."""
    actions = list(db.scalars(select(AgentAction).where(AgentAction.root_run_id == root_run_id)))
    actual = pending = Decimal("0")
    for action in actions:
        if action.gateway_call_id is None:
            continue
        call = db.get(GatewayCall, action.gateway_call_id)
        assert call is not None
        outcome = media_outcome(db, call) if call.generation_attempt_id else None
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
            raise DomainError("AGENT_LIMITS_INVALID", "受限调用缺少预算来源", 409)
    return {"actions": actions, "actual_cost": actual, "pending_estimate": pending}


def _check_action(
    db: Session, context: AgentExecutionContext, amount: Decimal = Decimal("0")
) -> AgentRunLimit:
    control = db.get(AgentRunLimit, context.run_id)
    if control is None:
        raise DomainError("AGENT_LIMITS_REQUIRED", "Agent 调用尚未建立运行限制", 409)
    limits = _active_chain(db, control, context.stage)
    usage = action_usage(db, control.root_run_id)
    actions: list[AgentAction] = usage["actions"]
    steps = {(row.run_id, row.step_key) for row in actions}
    kind_count = sum(row.kind == context.kind for row in actions)
    kind_maximum = limits.max_model_calls if context.kind == "MODEL" else limits.max_tool_calls
    kind_code = "AGENT_MODEL_CALL_LIMIT" if context.kind == "MODEL" else "AGENT_TOOL_CALL_LIMIT"
    checks = (
        (len(actions), limits.max_calls, "AGENT_CALL_LIMIT", "Agent 总调用次数已达上限"),
        (kind_count, kind_maximum, kind_code, "Agent 此类调用次数已达上限"),
        (
            sum(row.agent_name == context.agent_name for row in actions),
            limits.max_agent_calls,
            "AGENT_AGENT_CALL_LIMIT",
            "此 Agent 调用次数已达上限",
        ),
    )
    for used, maximum, code, message in checks:
        if used >= maximum:
            raise DomainError(code, message, 409)
    if (context.run_id, context.step_key) not in steps and len(steps) >= limits.max_steps:
        raise DomainError("AGENT_STEP_LIMIT", "Agent 决策步骤已达上限", 409)
    if (
        amount > 0
        and usage["actual_cost"] + usage["pending_estimate"] + amount > limits.maximum_cost
    ):
        raise DomainError("AGENT_BUDGET_EXCEEDED", "Agent 共享预算不足，已停止付费调用", 409)
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


def check_gateway_action(db: Session, request: GatewayRequest, route: GatewayRoute) -> None:
    """Called under the existing gateway gate, before its immutable reservation is written."""
    context = _gateway_context(db, request)
    # A safely unaccepted attempt may be retried, but its executor identity is frozen.
    # This also prevents a media retry from dropping its Agent limits entirely.
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
    if route.maximum_cost is None or route.pricing_version == "unconfigured":
        raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "受限 Agent 调用缺少可信报价", 409)
    control = _check_action(db, context, route.maximum_cost)
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


def gateway_dispatch_seconds(db: Session, request: GatewayRequest) -> float | None:
    """Recheck live cancellation/scope just before I/O, without consuming another slot."""
    from arclight.application.model_gateway import utc

    context = _gateway_context(db, request)
    if context is None:
        return None
    control = db.get(AgentRunLimit, context.run_id)
    if control is None:
        raise DomainError("AGENT_LIMITS_REQUIRED", "Agent 限制记录缺失", 409)
    _active_chain(db, control, context.stage)
    _check_paid_tool_grant(db, context, request)
    remaining = (utc(control.deadline_at) - utcnow()).total_seconds()
    if remaining <= 0:
        raise DomainError("AGENT_DEADLINE_EXCEEDED", "Agent 已达到总执行时限", 409)
    return remaining


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
