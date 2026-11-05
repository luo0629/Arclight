"""Durable Agent intent/result journal and fenced execution, separate from checkpoints."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_limits import action_usage, validate_agent_execution
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.gateway_accounting import latest_receipt
from arclight.application.gateway_text import execution_actor
from arclight.application.generation_leases import DEFAULT_POLICY, LeasePolicy, clock, utc
from arclight.application.model_gateway import lock_gate
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest
from arclight.domain.agent_runtime import AgentHumanDecision, HumanDecisionRequest
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import (
    AgentLoop,
    AgentLoopAction,
    AgentLoopDecision,
    AgentLoopQuestion,
    AgentLoopStamp,
)
from arclight.persistence.models import Episode, User, WorkflowRun


def checked_loop(db: Session, loop_id: UUID, *, actor: str | None = None) -> AgentLoop:
    """Always re-read current authority; a checkpoint cannot grant execution rights."""
    lock_gate(db)
    loop = db.get(AgentLoop, loop_id, populate_existing=True)
    if loop is None:
        raise DomainError("NOT_FOUND", "Agent 循环不存在", 404)
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == loop.run_id).with_for_update())
    episode = db.get(Episode, run.episode_id) if run else None
    if (
        run is None
        or episode is None
        or episode.project_id != loop.project_id
        or execution_actor(db, run) != loop.actor_id
        or scope_for_run(run, episode.project_id).input_sha256 != loop.input_sha256
    ):
        raise DomainError("AGENT_INPUT_CHANGED", "恢复作用域或冻结输入已变化", 409)
    if actor is not None:
        if actor != "service-owner":
            try:
                user = db.get(User, UUID(actor), populate_existing=True)
            except ValueError:
                user = None
            if user is None or user.disabled:
                raise DomainError("FORBIDDEN", "恢复操作者已不可执行", 403)
        require_project(db, loop.project_id, actor, write=True)
    seed = AgentModelRequest.model_validate(loop.seed_json)
    if seed.run_id != loop.run_id or digest(loop.seed_json) != loop.request_sha256:
        raise DomainError("AGENT_LOOP_INVALID", "持久运行合同无效", 409)
    limits = validate_agent_execution(db, run.id, seed.stage)
    control = db.get(AgentRunLimit, run.id)
    assert control is not None
    usage = action_usage(db, control.root_run_id)
    if usage["actual_cost"] + usage["pending_estimate"] > limits.maximum_cost:
        raise DomainError("AGENT_COST_LIMIT", "恢复时已有用量超过运行总预算", 409)
    return loop


def submit_agent_decision(
    db: Session, loop_id: UUID, actor: str, decision: AgentHumanDecision
) -> AgentLoopDecision:
    """Persist an authenticated answer once; this never approves an asset or dispatches I/O."""
    decision = AgentHumanDecision.model_validate(decision.model_dump(mode="json"))
    loop = checked_loop(db, loop_id, actor=actor)
    question = db.get(AgentLoopQuestion, decision.question_id)
    if (
        question is None
        or question.loop_id != loop.id
        or decision.input_sha256 != question.input_sha256
        or question.input_sha256 != loop.input_sha256
    ):
        raise DomainError("AGENT_STALE_DECISION", "问题或输入版本已变化", 409)
    value = decision.model_dump(mode="json")
    previous = db.scalar(
        select(AgentLoopDecision).where(AgentLoopDecision.question_id == question.id)
    )
    if previous is not None:
        if previous.actor_id == actor and previous.decision_json == value:
            return previous
        raise DomainError("AGENT_DECISION_CONFLICT", "此问题已有不同人工决定", 409)
    if not loop.outcome_json or loop.outcome_json.get("question_id") != str(question.id):
        raise DomainError("AGENT_STALE_DECISION", "此问题已不再等待决定", 409)
    if loop.status != "WAITING_HUMAN":
        raise DomainError("AGENT_NOT_WAITING", "循环当前不等待人工决定", 409)
    requested = HumanDecisionRequest.model_validate(question.question_json)
    if (
        requested.options
        and decision.decision == "ANSWER"
        and decision.answer not in requested.options
    ):
        raise DomainError("AGENT_DECISION_INVALID", "请选择当前问题中的一个选项", 422)
    row = AgentLoopDecision(question_id=question.id, actor_id=actor, decision_json=value)
    db.add(row)
    db.flush()
    audit(
        db,
        loop.project_id,
        actor,
        "agent.decision_recorded",
        loop.id,
        {
            "question_id": str(question.id),
            "decision_id": str(row.id),
            "decision": decision.decision,
        },
    )
    return row


class AgentLoopStore:
    def __init__(self, factory: sessionmaker[Session], loop_id: UUID, token: UUID):
        self.factory, self.loop_id, self.token = factory, loop_id, token

    def guard(self, db: Session, *, execution: bool = True) -> AgentLoop:
        loop: AgentLoop | None
        if execution:
            loop = checked_loop(db, self.loop_id)
        else:
            # Inbound results must survive cancellation/revocation; they grant no new action.
            lock_gate(db)
            loop = db.get(AgentLoop, self.loop_id, populate_existing=True)
        if (
            loop is None
            or loop.lease_token != self.token
            or loop.lease_expires_at is None
            or utc(loop.lease_expires_at) <= clock(db)
        ):
            raise DomainError("AGENT_EXECUTION_LOST", "Agent 执行权已失效", 409)
        return loop

    def plan(self, key: str, kind: str, request: dict) -> tuple[UUID, dict | None]:
        safe_metadata(request)
        with self.factory.begin() as db:
            self.guard(db)
            action = db.scalar(
                select(AgentLoopAction).where(
                    AgentLoopAction.loop_id == self.loop_id, AgentLoopAction.action_key == key
                )
            )
            if action is None:
                action = AgentLoopAction(
                    loop_id=self.loop_id,
                    action_key=key,
                    kind=kind,
                    request_sha256=digest(request),
                    request_json=request,
                    gateway_call_id=None,
                    result_json=None,
                )
                db.add(action)
                db.flush()
            if (
                action.kind != kind
                or action.request_sha256 != digest(request)
                or action.request_json != request
            ):
                raise DomainError("AGENT_ACTION_CONFLICT", "逻辑行动不能更换输入或合同", 409)
            if action.gateway_call_id is not None and action.result_json is None:
                receipt = latest_receipt(db, action.gateway_call_id)
                if receipt is None or receipt.status != "NOT_ACCEPTED":
                    raise DomainError(
                        "GATEWAY_RECONCILIATION_REQUIRED", "原模型行动结果未知，须沿网关对账", 409
                    )
            return action.id, action.result_json

    def bind_model(self, db: Session, action_id: UUID, call_id: UUID) -> None:
        self.guard(db)
        action = db.get(AgentLoopAction, action_id)
        assert action is not None and action.loop_id == self.loop_id and action.kind == "MODEL"
        if action.gateway_call_id is not None and action.gateway_call_id != call_id:
            receipt = latest_receipt(db, action.gateway_call_id)
            if receipt is None or receipt.status != "NOT_ACCEPTED":
                raise DomainError(
                    "GATEWAY_RECONCILIATION_REQUIRED", "原调用未确认未接收，禁止重发", 409
                )
        action.gateway_call_id = call_id

    def save_model(self, db: Session, action_id: UUID, call_id: UUID, response: dict) -> None:
        self.guard(db, execution=False)
        action = db.get(AgentLoopAction, action_id)
        assert (
            action is not None
            and action.gateway_call_id == call_id
            and action.loop_id == self.loop_id
        )
        safe_metadata(response)
        action.result_json = response

    def stamp(self, state: dict, changes: dict) -> dict:
        merged = state | changes
        safe_metadata(merged)
        checksum = digest(merged)
        with self.factory.begin() as db:
            self.guard(db, execution=False)
            previous = db.scalar(
                select(AgentLoopStamp.id).where(
                    AgentLoopStamp.loop_id == self.loop_id, AgentLoopStamp.state_sha256 == checksum
                )
            )
            if previous is None:
                db.add(AgentLoopStamp(loop_id=self.loop_id, state_sha256=checksum))
        return changes

    def verify(self, db: Session, state: dict) -> None:
        self.guard(db)
        if not db.scalar(
            select(AgentLoopStamp.id).where(
                AgentLoopStamp.loop_id == self.loop_id, AgentLoopStamp.state_sha256 == digest(state)
            )
        ):
            raise DomainError("AGENT_CHECKPOINT_INVALID", "checkpoint 内容未由此运行提交", 409)

    def question(self, turn: int, value: dict) -> UUID:
        with self.factory.begin() as db:
            loop = self.guard(db)
            row = db.scalar(
                select(AgentLoopQuestion).where(
                    AgentLoopQuestion.loop_id == loop.id, AgentLoopQuestion.model_turn == turn
                )
            )
            if row is None:
                row = AgentLoopQuestion(
                    loop_id=loop.id,
                    model_turn=turn,
                    input_sha256=loop.input_sha256,
                    question_json=value,
                )
                db.add(row)
                db.flush()
            if row.question_json != value or row.input_sha256 != loop.input_sha256:
                raise DomainError("AGENT_DECISION_CONFLICT", "同一行动不能改写人工问题", 409)
            return row.id

    def decision(self, question_id: UUID) -> tuple[UUID, dict, str] | None:
        with self.factory.begin() as db:
            self.guard(db)
            question = db.get(AgentLoopQuestion, question_id)
            if question is None or question.loop_id != self.loop_id:
                raise DomainError("AGENT_STALE_DECISION", "人工问题不属于此运行", 409)
            row = db.scalar(
                select(AgentLoopDecision).where(AgentLoopDecision.question_id == question_id)
            )
            return (row.id, row.decision_json, row.actor_id) if row is not None else None

    def finish(self, status: str, outcome: dict) -> None:
        with self.factory.begin() as db:
            loop = self.guard(db, execution=False)
            loop.status, loop.outcome_json = status, outcome


def claim_loop(db: Session, loop: AgentLoop, policy: LeasePolicy = DEFAULT_POLICY) -> UUID:
    stamp = clock(db)
    if (
        loop.lease_token is not None
        and loop.lease_expires_at is not None
        and utc(loop.lease_expires_at) > stamp
    ):
        raise DomainError("AGENT_LOOP_BUSY", "此循环已有有效执行者", 409)
    loop.lease_token = uuid4()
    loop.lease_expires_at = stamp + timedelta(seconds=policy.duration_seconds)
    return loop.lease_token


@contextmanager
def keep_loop_alive(store: AgentLoopStore, policy: LeasePolicy = DEFAULT_POLICY) -> Iterator[None]:
    stop = Event()

    def renew():
        while not stop.wait(policy.heartbeat_seconds):
            try:
                with store.factory.begin() as db:
                    loop = store.guard(db, execution=False)
                    loop.lease_expires_at = clock(db) + timedelta(seconds=policy.duration_seconds)
            except (SQLAlchemyError, DomainError):
                return

    thread = Thread(target=renew, name=f"agent-heartbeat-{store.loop_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        with store.factory.begin() as db:
            lock_gate(db)
            loop = db.get(AgentLoop, store.loop_id, populate_existing=True)
            if loop is not None and loop.lease_token == store.token:
                loop.lease_token = None
                loop.lease_expires_at = None
