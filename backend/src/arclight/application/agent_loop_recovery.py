"""Durable Pi execution, including human decisions and external tool readiness."""

import json
from collections.abc import Callable
from typing import cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, empty_checkpoint
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agent_skills import freeze_agent_skills
from arclight.application.agent_loop_store import (
    AgentLoopStore,
    checked_loop,
    claim_loop,
    keep_loop_alive,
    submit_agent_decision,
)
from arclight.application.agent_runtime import MAX_AGENT_LOOP_TURNS, _AgentRuntime
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    AgentToolRegistry,
    agent_tool_is_pending,
    execute_agent_tool,
)
from arclight.application.gateway_accounting import latest_receipt
from arclight.application.gateway_agent import AgentOutputProjection, invoke_agent
from arclight.application.generation_leases import DEFAULT_POLICY, LeasePolicy
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentModelResponse, AgentUserMessage
from arclight.domain.agent_runtime import (
    AgentDecision,
    AgentHumanDecision,
    AgentLoopResult,
    HumanDecisionRequest,
)
from arclight.domain.agent_tools import AgentToolCall, AgentToolFeedback
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.workflows.agent import AgentLoopState
from arclight.workflows.checkpoints import open_checkpointer

_INITIAL_QUESTION = "_server_initial_question"
_SESSION_NAMESPACE = "pi-agent"
_SESSION_CHANNEL = "state"
_LEGACY_FIELDS = frozenset({"request", "observed_round", "model_turns", "response", "outcome"})
_LEGACY_BRANCH = "branch:to:"


def with_initial_question(
    request: AgentModelRequest, question: HumanDecisionRequest
) -> AgentModelRequest:
    """Freeze a server-required pause in a new operation, before any model call.

    This reserved control message is constructed by application services, never by
    an HTTP story/answer body. It is part of the exact journal seed and hash.
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
    def __init__(self, state: AgentLoopState, code: str):
        super().__init__(code, "原行动等待恢复", 409)
        self.state = state


class _ExternalWait(Exception):
    def __init__(self, request: AgentToolCall):
        self.request = request


class _SessionCheckpoint:
    """Store a single Pi resume cursor in the existing, separate checkpoint database."""

    def __init__(self, store: AgentLoopStore, saver: BaseCheckpointSaver[str], thread: str):
        self.store = store
        self.saver = saver
        self.thread = thread
        self.config: RunnableConfig = {
            "configurable": {"thread_id": thread, "checkpoint_ns": _SESSION_NAMESPACE}
        }
        self.step = -1
        self.version: str | None = None

    def load(self) -> AgentLoopState | None:
        snapshot = self.saver.get_tuple(self.config)
        if snapshot is None:
            return None
        state = snapshot.checkpoint["channel_values"].get(_SESSION_CHANNEL)
        version = snapshot.checkpoint["channel_versions"].get(_SESSION_CHANNEL)
        if not isinstance(state, dict) or not isinstance(version, str):
            raise DomainError("AGENT_CHECKPOINT_INVALID", "Agent 恢复状态不完整", 409)
        with self.store.factory.begin() as db:
            self.store.verify(db, state)
        self.config = snapshot.config
        self.step = snapshot.metadata.get("step", 0)
        self.version = version
        return cast(AgentLoopState, state)

    def legacy(self) -> CheckpointTuple | None:
        return self.saver.get_tuple(
            {"configurable": {"thread_id": self.thread, "checkpoint_ns": ""}}
        )

    def save(self, state: AgentLoopState) -> None:
        checkpoint = empty_checkpoint()
        self.store.stamp({}, dict(state))
        version = self.saver.get_next_version(self.version, None)
        checkpoint["channel_values"] = {_SESSION_CHANNEL: dict(state)}
        checkpoint["channel_versions"] = {_SESSION_CHANNEL: version}
        checkpoint["updated_channels"] = [_SESSION_CHANNEL]
        self.config = self.saver.put(
            self.config,
            checkpoint,
            {"source": "loop", "step": self.step + 1, "parents": {}},
            {_SESSION_CHANNEL: version},
        )
        self.step += 1
        self.version = version


class _PersistentRuntime[T: BaseModel](_AgentRuntime[T]):
    store: AgentLoopStore
    input_sha256: str
    session: _SessionCheckpoint

    def _context(self, db: Session):
        context = super()._context(db)
        self.store.guard(db)
        return context

    def _outcome(self, state: AgentLoopState, **values) -> dict:
        return super()._outcome(
            state, loop_id=self.store.loop_id, input_sha256=self.input_sha256, **values
        )

    def _stop(self, state: AgentLoopState, code: str) -> AgentLoopState:
        if code == "PRODUCER_PAUSED":
            # A reversible admission pause must not become a terminal journal outcome.
            raise DomainError(code, "多集制片暂停，原行动等待恢复", 409)
        if code in {"GATEWAY_RECONCILIATION_REQUIRED", "ACCOUNT_BALANCE_INSUFFICIENT"}:
            # Keep the pending action and its input. Reentry requires a NOT_ACCEPTED receipt.
            raise _RecoveryRequired(state, code)
        return super()._stop(state, code)

    def checkpoint(self, state: AgentLoopState) -> None:
        self.session.save(state)

    def finish(self, state: AgentLoopState) -> AgentLoopState:
        changes = super().finish(state)
        outcome = changes.get("outcome")
        if outcome is not None and outcome["status"] == "HUMAN_REVIEW":
            identity = self.store.question(state["model_turns"], outcome["human_request"])
            changes["outcome"] = outcome | {"question_id": str(identity)}
        return changes

    def invoke_model(
        self, request: AgentModelRequest, turn: int
    ) -> AgentModelResponse[AgentDecision[T]]:
        declarations = []
        for tool in self.declarations:
            schema = tool.parameters
            parameters: dict | str = schema
            try:
                safe_metadata(schema)
            except ValueError:
                # Preserve original request hashes and keep sensitive values out of the journal.
                parameters = json.dumps(schema, ensure_ascii=False, sort_keys=True)
            declarations.append({"name": tool.name, "parameters": parameters})
        key = self._execution(turn).step_key
        action_id, saved = self.store.plan(
            key, "MODEL", {"request": request.model_dump(mode="json"), "tools": declarations}
        )
        if saved is not None:
            response = self.response_model.model_validate(saved)
            with self.factory.begin() as db:
                self.store.guard(db)
                action = db.get(AgentLoopAction, action_id)
                assert action is not None and action.gateway_call_id is not None
                receipt = latest_receipt(db, action.gateway_call_id)
                assert receipt is not None
                usage = GatewayUsage.model_validate(receipt.usage_json)
            self.pi.accept_model(response, tuple(tool.name for tool in self.declarations), usage)
            return response
        return invoke_agent(
            self.factory,
            self.settings,
            request,
            self.registry,
            self.decision_model,
            execution=self._execution(turn),
            output_projection=self.output_projection,
            on_admit=lambda db, call_id: self.store.bind_model(db, action_id, call_id),
            on_result=lambda db, call_id, result: self.store.save_model(
                db, action_id, call_id, result.model_dump(mode="json")
            ),
            on_round=self.pi.accept_model,
            expected_tools=self.declarations,
        )

    def execute_tool(
        self, request: AgentToolCall, step_key: str, *, rejected: bool = False
    ) -> AgentToolFeedback:
        action_id, saved = self.store.plan(
            request.idempotency_key, "TOOL", request.model_dump(mode="json")
        )
        wait_key = "wait-" + digest({"tool_action": request.idempotency_key})
        if saved is None and not rejected and self.tool_pending(request):
            self.store.plan(wait_key, "WAIT", request.model_dump(mode="json"))
            raise _ExternalWait(request)
        with self.factory.begin() as db:
            self._context(db)
            feedback = execute_agent_tool(
                db,
                self.registry,
                self.seed.agent_name,
                request,
                step_key=step_key,
                rejected=rejected,
            )
            action = db.get(AgentLoopAction, action_id)
            assert action is not None
            value = feedback.model_dump(mode="json")
            if saved is None:
                action.result_json = value
            elif saved != value and feedback.status == "SUCCEEDED":
                raise DomainError("AGENT_ACTION_CONFLICT", "工具回放与持久结果不一致", 409)
            return feedback

    def tool_pending(self, request: AgentToolCall, *, resuming: bool = False) -> bool:
        with self.factory.begin() as db:
            _, _, actor = self._context(db)
            if resuming:
                intent = db.scalar(
                    select(AgentLoopAction).where(
                        AgentLoopAction.loop_id == self.store.loop_id,
                        AgentLoopAction.action_key == request.idempotency_key,
                        AgentLoopAction.kind == "TOOL",
                    )
                )
                if intent is None or intent.request_json != request.model_dump(mode="json"):
                    raise DomainError("AGENT_ACTION_CONFLICT", "外部等待与持久行动不一致", 409)
            pending = agent_tool_is_pending(
                db, self.registry, self.seed.agent_name, request, actor, self.seed.stage
            )
            if resuming and not pending:
                # The current delivery owns recovery, even if the new Pi process exits.
                loop = self.store.guard(db)
                loop.status, loop.outcome_json = "RUNNING", None
        return pending

    def human(self, state: AgentLoopState) -> AgentLoopState:
        outcome = state["outcome"]
        assert outcome is not None
        question_id = UUID(outcome["question_id"])
        stored = self.store.decision(question_id)
        if stored is None:
            return state
        decision_id, value, actor = stored
        decision = AgentHumanDecision.model_validate(value)
        if decision.decision == "STOP":
            return state | self._stop(state, "AGENT_HUMAN_STOPPED")
        request = AgentModelRequest.model_validate(state["request"])
        try:
            answer = AgentUserMessage(
                data={
                    "human_decision": {
                        "decision_id": str(decision_id),
                        "actor_id": actor,
                        "question": outcome["human_request"],
                        **value,
                    }
                }
            )
            updated = AgentModelRequest.model_validate(
                request.model_dump(mode="json")
                | {
                    "messages": [
                        item.model_dump(mode="json") for item in [*request.messages, answer]
                    ]
                }
            )
        except ValidationError:
            return state | self._stop(state, "AGENT_CONTEXT_LIMIT")
        return state | {
            "request": updated.model_dump(mode="json"),
            "outcome": None,
            "phase": "prepare",
        }


def _import_legacy_state(runtime: _PersistentRuntime, snapshot: CheckpointTuple) -> AgentLoopState:
    """Translate a stamped graph checkpoint into the current Pi cursor once."""
    channels = snapshot.checkpoint["channel_values"]
    values = {key: value for key, value in channels.items() if key in _LEGACY_FIELDS}
    branch = next(
        (key.removeprefix(_LEGACY_BRANCH) for key in channels if key.startswith(_LEGACY_BRANCH)), ""
    )
    if not values and isinstance(channels.get("__start__"), dict):
        values = dict(channels["__start__"])
        branch = "observe"
    # A completed graph node may have committed its writes before the process
    # saved the next checkpoint. Only its stamped business fields carry forward.
    writes = {name: value for _, name, value in snapshot.pending_writes or ()}
    if _LEGACY_FIELDS.intersection(writes):
        values.update({key: value for key, value in writes.items() if key in _LEGACY_FIELDS})
        branch = next(
            (key.removeprefix(_LEGACY_BRANCH) for key in writes if key.startswith(_LEGACY_BRANCH)),
            "",
        )
    with runtime.factory.begin() as db:
        runtime.store.verify(db, values)
    state = cast(AgentLoopState, values)
    if state.get("outcome") is not None:
        state["phase"] = "finish"
        return state
    if branch == "observe" or (branch == "decide" and state["model_turns"] >= MAX_AGENT_LOOP_TURNS):
        state.update({"phase": "prepare", "response": None})
        return state
    if branch not in {"decide", "tools", "finish"}:
        raise DomainError("AGENT_CHECKPOINT_INVALID", "无法恢复 Agent 旧检查点", 409)
    if branch == "decide":
        state["model_turns"] += 1
    state["request"] = state["request"] | {
        "operation_key": runtime._execution(state["model_turns"]).step_key
    }
    state["phase"] = "tools" if branch == "tools" else "model"
    if branch != "tools":
        state["response"] = None
    return state


def _start_state(runtime: _PersistentRuntime) -> AgentLoopState:
    legacy = runtime.session.legacy()
    if legacy is not None:
        return _import_legacy_state(runtime, legacy)
    state = AgentLoopState(
        request=runtime.seed.model_dump(mode="json"),
        observed_round=-1,
        model_turns=0,
        phase="prepare",
    )
    question = _initial_question(runtime.seed)
    if question is not None:
        value = question.model_dump(mode="json")
        state["phase"] = "finish"
        state["outcome"] = runtime._outcome(
            state,
            status="HUMAN_REVIEW",
            human_request=value,
            question_id=str(runtime.store.question(0, value)),
        )
    return state


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
        thread = loop.thread_id
        previous_status, previous_outcome = loop.status, loop.outcome_json
    persistent = _PersistentRuntime(
        runtime.factory,
        runtime.settings,
        runtime.seed,
        runtime.registry,
        runtime.result_model,
        runtime.validate_result,
        runtime.output_projection,
        include_project_reads=runtime.include_project_reads,
    )
    persistent.store = store = AgentLoopStore(runtime.factory, loop_id, token)
    persistent.input_sha256 = input_sha256
    persistent.project_id = runtime.project_id
    with keep_loop_alive(store, lease_policy), open_checkpointer(runtime.settings) as saver:
        persistent.session = _SessionCheckpoint(store, saver, thread)
        state = persistent.session.load()
        if state is None:
            state = _start_state(persistent)
            persistent.checkpoint(state)
        if previous_status == "WAITING_EXTERNAL":
            waiting = persistent.outcome_model.model_validate(previous_outcome)
            assert waiting.waiting_for is not None
            if persistent.tool_pending(waiting.waiting_for, resuming=True):
                return waiting
        if (state.get("outcome") or {}).get("status") == "HUMAN_REVIEW":
            continued = persistent.human(state)
            if continued is not state:
                persistent.checkpoint(continued)
                state = continued
        if previous_status == "WAITING_HUMAN" and state.get("outcome") is None:
            with runtime.factory.begin() as db:
                active_loop = store.guard(db)
                active_loop.status, active_loop.outcome_json = "RUNNING", None
        try:
            if state.get("outcome") is None:
                state = persistent.run(state)
            outcome = state["outcome"]
            assert outcome is not None
            status = "WAITING_HUMAN" if outcome["status"] == "HUMAN_REVIEW" else outcome["status"]
        except _ExternalWait as exc:
            status = "WAITING_EXTERNAL"
            outcome = persistent._outcome(
                state, status=status, waiting_for=exc.request.model_dump(mode="json")
            )
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
    parent_run_id: UUID | None = None,
    validate_result: Callable[[Session, T], None] | None = None,
    output_projection: AgentOutputProjection[AgentDecision[T]] | None = None,
    include_project_reads: bool = True,
    lease_policy: LeasePolicy = DEFAULT_POLICY,
) -> AgentLoopResult[T]:
    """Start/reenter one stable server operation. No asset is published or approved."""
    request = AgentModelRequest.model_validate(request.model_dump(mode="json"))
    if any(not isinstance(item, AgentUserMessage) for item in request.messages):
        raise DomainError("AGENT_HISTORY_INVALID", "新循环不能注入模型或工具历史", 422)
    runtime = _AgentRuntime(
        factory,
        settings,
        request,
        registry,
        result_model,
        validate_result,
        output_projection,
        include_project_reads=include_project_reads,
    )
    runtime.prepare(parent_run_id)
    with factory.begin() as db:
        run, project_id, actor = runtime._context(db)
        loop = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id, AgentLoop.operation_sha256 == runtime.identity
            )
        )
        schema = digest(result_model.model_json_schema())
        if loop is None:
            seed = freeze_agent_skills(request)
            seed_json = seed.model_dump(mode="json")
            identity = uuid4()
            loop = AgentLoop(
                id=identity,
                run_id=run.id,
                project_id=project_id,
                actor_id=actor,
                operation_sha256=runtime.identity,
                request_sha256=digest(seed_json),
                input_sha256=scope_for_run(run, project_id).input_sha256,
                result_schema_sha256=schema,
                thread_id=f"agent-loop:{identity}",
                seed_json=seed_json,
                status="RUNNING",
            )
            db.add(loop)
            db.flush()
        else:
            # Reentry supplies the same business request. The stored seed owns the
            # exact instructions, including operations sealed before Skill loading.
            seed = AgentModelRequest.model_validate(loop.seed_json)
            if loop.result_schema_sha256 != schema or digest(
                seed.model_dump(mode="json", exclude={"skills", "max_tokens"})
            ) != digest(request.model_dump(mode="json", exclude={"skills", "max_tokens"})):
                raise DomainError("AGENT_LOOP_CONFLICT", "同一操作不能更换请求或结果合同", 409)
        loop_id = loop.id
    runtime.seed = seed
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
    output_projection: AgentOutputProjection[AgentDecision[T]] | None = None,
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
    runtime = _AgentRuntime(
        factory, settings, seed, registry, result_model, validate_result, output_projection
    )
    runtime.prepare(None)
    return _execute(runtime, loop_id, actor=actor, lease_policy=lease_policy)
