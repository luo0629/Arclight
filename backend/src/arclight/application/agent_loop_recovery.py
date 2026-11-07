"""Persistent execution of the shared Agent graph, including native human interrupts."""

import json
from collections.abc import Callable
from typing import cast
from uuid import UUID, uuid4

from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import (
    AgentLoopStore,
    checked_loop,
    claim_loop,
    keep_loop_alive,
    submit_agent_decision,
)
from arclight.application.agent_runtime import _AgentRuntime
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    AgentToolRegistry,
    available_agent_tools,
    execute_agent_tool,
)
from arclight.application.gateway_agent import invoke_agent
from arclight.application.generation_leases import DEFAULT_POLICY, LeasePolicy
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentModelResponse, AgentUserMessage
from arclight.domain.agent_runtime import (
    AgentDecision,
    AgentHumanDecision,
    AgentLoopResult,
    HumanDecisionRequest,
)
from arclight.domain.agent_tools import AgentToolCall, AgentToolFeedback
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.workflows.agent import AgentLoopState, build_agent_graph
from arclight.workflows.checkpoints import open_checkpointer

_INITIAL_QUESTION = "_server_initial_question"


def with_initial_question(
    request: AgentModelRequest, question: HumanDecisionRequest
) -> AgentModelRequest:
    """Freeze a server-required pause in a new operation, before any model call.

    This reserved control message is constructed by application services, never by
    an HTTP story/answer body. It is part of the exact journal seed and hash.
    Existing operations and their graph entry points remain unchanged.
    """
    control = AgentUserMessage(data={_INITIAL_QUESTION: question.model_dump(mode="json")})
    return AgentModelRequest.model_validate(
        request.model_dump(mode="json")
        | {"messages": [item.model_dump(mode="json") for item in [*request.messages, control]]}
    )


def _initial_question(request: AgentModelRequest) -> HumanDecisionRequest | None:
    last = request.messages[-1]
    if isinstance(last, AgentUserMessage) and set(last.data) == {_INITIAL_QUESTION}:
        return HumanDecisionRequest.model_validate(last.data[_INITIAL_QUESTION])
    return None


class _RecoveryRequired(DomainError):
    def __init__(self, state: AgentLoopState):
        super().__init__("GATEWAY_RECONCILIATION_REQUIRED", "原行动须对账后恢复", 409)
        self.state = state


class _PersistentRuntime[T: BaseModel](_AgentRuntime[T]):
    store: AgentLoopStore
    input_sha256: str

    def _context(self, db: Session):
        context = super()._context(db)
        self.store.guard(db)
        return context

    def _outcome(self, state: AgentLoopState, **values) -> dict:
        return super()._outcome(
            state, loop_id=self.store.loop_id, input_sha256=self.input_sha256, **values
        )

    def _stop(self, state: AgentLoopState, code: str) -> AgentLoopState:
        if code == "GATEWAY_RECONCILIATION_REQUIRED":
            # Do not checkpoint a terminal result over the pending action. After explicit
            # NOT_ACCEPTED evidence, the same logical action may safely continue.
            raise _RecoveryRequired(state)
        return super()._stop(state, code)

    def _stamped(self, state: AgentLoopState, changes: AgentLoopState) -> AgentLoopState:
        return cast(AgentLoopState, self.store.stamp(dict(state), dict(changes)))

    def observe(self, state: AgentLoopState) -> AgentLoopState:
        return self._stamped(state, super().observe(state))

    def decide(self, state: AgentLoopState) -> AgentLoopState:
        return self._stamped(state, super().decide(state))

    def tools(self, state: AgentLoopState) -> AgentLoopState:
        return self._stamped(state, super().tools(state))

    def finish(self, state: AgentLoopState) -> AgentLoopState:
        changes = super().finish(state)
        outcome = changes.get("outcome")
        if outcome is not None and outcome["status"] == "HUMAN_REVIEW":
            identity = self.store.question(state["model_turns"], outcome["human_request"])
            changes["outcome"] = outcome | {"question_id": str(identity)}
        return self._stamped(state, changes)

    def invoke_model(
        self, request: AgentModelRequest, turn: int
    ) -> AgentModelResponse[AgentDecision[T]]:
        with self.factory.begin() as db:
            _, _, actor = self._context(db)
            definitions = available_agent_tools(
                db, self.registry, request.run_id, request.agent_name, actor, request.stage
            )
            declarations = []
            for tool in definitions:
                schema = tool.parameters_model.model_json_schema()
                parameters: dict | str = schema
                try:
                    safe_metadata(schema)
                except ValueError:
                    # Trusted schemas may name data such as an authorization query.
                    # Encode their grammar, never relax credential checks on values.
                    # Keep the original shape for already-persisted safe declarations.
                    parameters = json.dumps(schema, ensure_ascii=False, sort_keys=True)
                declarations.append({"name": tool.name, "parameters": parameters})
        key = self._execution(turn).step_key
        action_id, saved = self.store.plan(
            key, "MODEL", {"request": request.model_dump(mode="json"), "tools": declarations}
        )
        if saved is not None:
            return self.response_model.model_validate(saved)
        return invoke_agent(
            self.factory,
            self.settings,
            request,
            self.registry,
            self.decision_model,
            execution=self._execution(turn),
            on_admit=lambda db, call_id: self.store.bind_model(db, action_id, call_id),
            on_result=lambda db, call_id, result: self.store.save_model(
                db, action_id, call_id, result.model_dump(mode="json")
            ),
        )

    def execute_tool(self, request: AgentToolCall, step_key: str) -> AgentToolFeedback:
        action_id, saved = self.store.plan(
            request.idempotency_key, "TOOL", request.model_dump(mode="json")
        )
        with self.factory.begin() as db:
            self._context(db)
            # The existing executor rechecks current grants even when replaying a receipt.
            feedback = execute_agent_tool(
                db, self.registry, self.seed.agent_name, request, step_key=step_key
            )
            action = db.get(AgentLoopAction, action_id)
            assert action is not None
            value = feedback.model_dump(mode="json")
            if saved is None:
                action.result_json = value
            elif saved != value and feedback.status == "SUCCEEDED":
                raise DomainError("AGENT_ACTION_CONFLICT", "工具回放与持久结果不一致", 409)
            return feedback

    def human(self, state: AgentLoopState) -> AgentLoopState:
        try:
            return self._human_answer(state)
        except ValidationError:
            return self._stamped(state, self._stop(state, "AGENT_CONTEXT_LIMIT"))

    def _human_answer(self, state: AgentLoopState) -> AgentLoopState:
        outcome = state["outcome"]
        assert outcome is not None
        question_id = UUID(outcome["question_id"])
        # No effects before interrupt: this node restarts when Command resumes it.
        resumed = interrupt(
            {
                "loop_id": str(self.store.loop_id),
                "question_id": str(question_id),
                "input_sha256": self.input_sha256,
                "human_request": outcome["human_request"],
            }
        )
        stored = self.store.decision(question_id)
        if stored is None or str(stored[0]) != resumed:
            raise DomainError("AGENT_DECISION_REQUIRED", "恢复必须使用已持久化的人工决定", 409)
        decision_id, value, actor = stored
        decision = AgentHumanDecision.model_validate(value)
        if decision.decision == "STOP":
            return self._stamped(state, self._stop(state, "AGENT_HUMAN_STOPPED"))
        request = AgentModelRequest.model_validate(state["request"])
        updated = AgentModelRequest.model_validate(
            request.model_dump(mode="json")
            | {
                "messages": [item.model_dump(mode="json") for item in request.messages]
                + [
                    AgentUserMessage(
                        data={
                            "human_decision": {
                                "decision_id": str(decision_id),
                                "actor_id": actor,
                                "question": outcome["human_request"],
                                **value,
                            }
                        }
                    ).model_dump(mode="json")
                ]
            }
        )
        return self._stamped(state, {"request": updated.model_dump(mode="json"), "outcome": None})


def _execute[T: BaseModel](
    runtime: _AgentRuntime[T],
    loop_id: UUID,
    *,
    actor: str | None,
    lease_policy: LeasePolicy,
) -> AgentLoopResult[T]:
    with runtime.factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        if loop.status in {"COMPLETED", "STOPPED"}:
            result = runtime.outcome_model.model_validate(loop.outcome_json)
            if result.result is not None and runtime.validate_result is not None:
                runtime.validate_result(db, result.result)
            return result
        token = claim_loop(db, loop, lease_policy)
        input_sha256 = loop.input_sha256
    persistent = _PersistentRuntime(
        runtime.factory,
        runtime.settings,
        runtime.seed,
        runtime.registry,
        runtime.result_model,
        runtime.validate_result,
    )
    persistent.store = store = AgentLoopStore(runtime.factory, loop_id, token)
    persistent.input_sha256 = input_sha256
    persistent.project_id, persistent.limits = runtime.project_id, runtime.limits
    config = {
        "configurable": {"thread_id": f"agent-loop:{loop_id}"},
        "recursion_limit": min(runtime.limits.max_steps, runtime.limits.max_model_calls) * 4 + 10,
    }
    with keep_loop_alive(store, lease_policy), open_checkpointer(runtime.settings) as saver:
        initial_question = _initial_question(runtime.seed)
        graph = build_agent_graph(
            persistent,
            checkpointer=saver,
            human=persistent.human,
            start_with_human=initial_question is not None,
        )
        snapshot = graph.get_state(config)
        value: AgentLoopState | Command | None
        if snapshot.values:
            with runtime.factory.begin() as db:
                store.verify(db, snapshot.values)
            value = None
            if snapshot.interrupts:
                waiting = snapshot.values["outcome"]
                stored = store.decision(UUID(waiting["question_id"]))
                if stored is None:
                    store.finish("WAITING_HUMAN", waiting)
                    return persistent.outcome_model.model_validate(waiting)
                value = Command(resume=str(stored[0]))
        else:
            value = AgentLoopState(
                request=runtime.seed.model_dump(mode="json"), observed_round=-1, model_turns=0
            )
            if initial_question is not None:
                question = initial_question.model_dump(mode="json")
                value["outcome"] = persistent._outcome(
                    value,
                    status="HUMAN_REVIEW",
                    human_request=question,
                    question_id=str(store.question(0, question)),
                )
            store.stamp({}, dict(value))
        try:
            state = graph.invoke(value, config, durability="sync")
            outcome = state["outcome"]
            assert outcome is not None
            status = "WAITING_HUMAN" if outcome["status"] == "HUMAN_REVIEW" else outcome["status"]
        except _RecoveryRequired as exc:
            outcome = persistent._outcome(exc.state, status="STOPPED", stop_code=exc.code)
            status = "RECOVERY_REQUIRED"
        store.finish(status, outcome)
        return persistent.outcome_model.model_validate(outcome)


def run_persistent_agent_loop[T: BaseModel](
    factory: sessionmaker[Session],
    settings: Settings,
    request: AgentModelRequest,
    registry: AgentToolRegistry,
    result_model: type[T],
    *,
    limits: AgentRunLimits | None = None,
    parent_run_id: UUID | None = None,
    validate_result: Callable[[Session, T], None] | None = None,
    lease_policy: LeasePolicy = DEFAULT_POLICY,
) -> AgentLoopResult[T]:
    """Start/reenter one stable server operation. No asset is published or approved."""
    request = AgentModelRequest.model_validate(request.model_dump(mode="json"))
    if any(not isinstance(item, AgentUserMessage) for item in request.messages):
        raise DomainError("AGENT_HISTORY_INVALID", "新循环不能注入模型或工具历史", 422)
    runtime = _AgentRuntime(factory, settings, request, registry, result_model, validate_result)
    runtime.prepare(limits, parent_run_id)
    with factory.begin() as db:
        run, project_id, actor = runtime._context(db)
        loop = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id, AgentLoop.operation_sha256 == runtime.identity
            )
        )
        checksum, schema = (
            digest(request.model_dump(mode="json")),
            digest(result_model.model_json_schema()),
        )
        if loop is None:
            identity = uuid4()
            loop = AgentLoop(
                id=identity,
                run_id=run.id,
                project_id=project_id,
                actor_id=actor,
                operation_sha256=runtime.identity,
                request_sha256=checksum,
                input_sha256=scope_for_run(run, project_id).input_sha256,
                result_schema_sha256=schema,
                thread_id=f"agent-loop:{identity}",
                seed_json=request.model_dump(mode="json"),
                status="RUNNING",
            )
            db.add(loop)
            db.flush()
        if loop.request_sha256 != checksum or loop.result_schema_sha256 != schema:
            raise DomainError("AGENT_LOOP_CONFLICT", "同一操作不能更换请求或结果合同", 409)
        loop_id = loop.id
    return _execute(runtime, loop_id, actor=None, lease_policy=lease_policy)


def resume_agent_loop[T: BaseModel](
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    registry: AgentToolRegistry,
    result_model: type[T],
    *,
    decision: AgentHumanDecision | None = None,
    validate_result: Callable[[Session, T], None] | None = None,
    lease_policy: LeasePolicy = DEFAULT_POLICY,
) -> AgentLoopResult[T]:
    """Resume stored state only, after reauthorizing both initiator and current actor."""
    with factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        if loop.result_schema_sha256 != digest(result_model.model_json_schema()):
            raise DomainError("AGENT_LOOP_CONFLICT", "恢复不能更换结果合同", 409)
        seed = AgentModelRequest.model_validate(loop.seed_json)
        if decision is not None:
            submit_agent_decision(db, loop.id, actor, decision)
    runtime = _AgentRuntime(factory, settings, seed, registry, result_model, validate_result)
    runtime.prepare(None, None)
    return _execute(runtime, loop_id, actor=actor, lease_policy=lease_policy)
