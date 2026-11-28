"""Serve Pi model and tool callbacks inside the owning workflow scope."""

import json
from collections.abc import Callable
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agent_skills import freeze_agent_skills
from arclight.application.agent_limits import configure_agent_run, validate_agent_execution
from arclight.application.agent_memory import project_agent_memory
from arclight.application.agent_runtime.loop_state import AgentLoopState
from arclight.application.agent_runtime.pi_bridge import PiAgent, PiRuntimeError
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import (
    AgentToolRegistry,
    available_agent_tools,
    execute_agent_tool,
)
from arclight.application.context_evidence import read_trace
from arclight.application.gateway_agent import AgentOutputProjection, invoke_agent
from arclight.application.gateway_text import execution_actor
from arclight.application.model_gateway import lock_gate
from arclight.application.shared_agent_reads import agent_tool_declarations, with_project_reads
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext
from arclight.domain.agent_model import (
    AgentModelRequest,
    AgentModelResponse,
    AgentToolDeclaration,
    AgentUserMessage,
)
from arclight.domain.agent_runtime import AgentDecision, AgentLoopResult
from arclight.domain.agent_state import state_for_scope, transition_agent_step
from arclight.domain.agent_tools import AgentToolCall, AgentToolFeedback, RetrieveEvidenceResult
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import Episode, WorkflowRun

MAX_AGENT_LOOP_TURNS = 32
_FATAL_TOOL_ERRORS = frozenset(
    {
        "USER_NOT_AUTHORIZED",
        "UNKNOWN_TOOL",
        "AGENT_NOT_ALLOWED",
        "WORKFLOW_NOT_ACTIVE",
        "RUN_STATE_INVALID",
        "INVOCATION_RECORD_INVALID",
        "IDEMPOTENCY_CONFLICT",
        "FORBIDDEN",
        "PROJECT_ARCHIVED",
    }
)


class _AgentRuntime[T: BaseModel]:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        seed: AgentModelRequest,
        registry: AgentToolRegistry,
        result_model: type[T],
        validate_result: Callable[[Session, T], None] | None,
        output_projection: AgentOutputProjection[AgentDecision[T]] | None = None,
        *,
        include_project_reads: bool = True,
        complete_after_tools: Callable[[Session], T | None] | None = None,
    ):
        self.factory, self.settings, self.seed = factory, settings, seed
        self.pi = PiAgent(seed)
        self.declarations: tuple[AgentToolDeclaration, ...] = ()
        self.include_project_reads = include_project_reads
        self.registry = with_project_reads(registry) if include_project_reads else registry
        self.result_model = result_model
        self.decision_model = cast(
            type[AgentDecision[T]], AgentDecision.__class_getitem__(result_model)
        )
        self.response_model = cast(
            type[AgentModelResponse[AgentDecision[T]]],
            AgentModelResponse.__class_getitem__(self.decision_model),
        )
        self.outcome_model = cast(
            type[AgentLoopResult[T]], AgentLoopResult.__class_getitem__(result_model)
        )
        self.validate_result = validate_result
        self.output_projection = output_projection
        self.complete_after_tools = complete_after_tools
        self.identity = digest(
            {"run": str(seed.run_id), "stage": seed.stage, "operation": seed.operation_key}
        )

    def _context(self, db: Session) -> tuple[WorkflowRun, UUID, str]:
        # Preserve the gateway/tool lock order. These short transactions never span I/O.
        lock_gate(db)
        run = db.scalar(
            select(WorkflowRun).where(WorkflowRun.id == self.seed.run_id).with_for_update()
        )
        episode = db.get(Episode, run.episode_id) if run else None
        if run is None or episode is None:
            raise DomainError("NOT_FOUND", "Agent 运行不存在", 404)
        return run, episode.project_id, execution_actor(db, run)

    def _execution(self, turn: int) -> AgentExecutionContext:
        return AgentExecutionContext.model_validate(
            {
                "run_id": self.seed.run_id,
                "agent_name": self.seed.agent_name,
                "stage": self.seed.stage,
                "step_key": f"loop:{self.identity}:{turn}",
            }
        )

    def _stop(self, state: AgentLoopState, code: str) -> AgentLoopState:
        return {"outcome": self._outcome(state, status="STOPPED", stop_code=code)}

    def _outcome(self, state: AgentLoopState, **values) -> dict:
        return self.outcome_model(
            run_id=self.seed.run_id,
            project_id=self.project_id,
            agent_name=self.seed.agent_name,
            stage=self._execution(1).stage,
            model_turns=state["model_turns"],
            **values,
        ).model_dump(mode="json")

    def prepare(self, parent_run_id: UUID | None) -> None:
        self._execution(1)  # Validate the server-owned stage before configuring a run.
        with self.factory.begin() as db:
            run, self.project_id, actor = self._context(db)
            previous = db.get(AgentRunLimit, run.id)
            configure_agent_run(
                db,
                run.id,
                actor,
                parent_run_id=parent_run_id
                if parent_run_id is not None
                else (previous.parent_run_id if previous else None),
            )
            validate_agent_execution(db, run.id, self.seed.stage)

    def observe(self, state: AgentLoopState) -> AgentLoopState:
        try:
            request = AgentModelRequest.model_validate(state["request"])
            with self.factory.begin() as db:
                run, project_id, actor = self._context(db)
                validate_agent_execution(db, run.id, request.stage)
                trace = read_trace(db, run, request.stage, actor)
                count = len(trace["evidence"])
                if count < state["observed_round"]:
                    raise DomainError("CONTEXT_REFERENCE_INVALID", "Agent 证据链不能回退", 409)
                scope = scope_for_run(run, project_id)
                assert run.agent_state_json is not None  # validated by the admission scope
                current = state_for_scope(run.agent_state_json, scope)
                updated = transition_agent_step(
                    current,
                    scope,
                    expected_revision=current.revision,
                    step_id=self._execution(1).stage,
                    target="RUNNING",
                    observation_ids=tuple(
                        UUID(value)
                        for value in (
                            [trace["startup_manifest_id"]]
                            + [item["manifest_id"] for item in trace["evidence"]]
                        )
                    ),
                )
                run.agent_state_json = updated.model_dump(mode="json")
                first = self.seed.messages[0]
                start = (
                    first.data.get("context_evidence_start", 0)
                    if isinstance(first, AgentUserMessage)
                    else 0
                )
                if type(start) is not int or not 0 <= start <= count:
                    raise DomainError("CONTEXT_REFERENCE_INVALID", "操作证据起点无效", 409)
                # The immutable global trace covers all operations.
                # A fresh operation consumes its own evidence plus the required
                # startup facts; its seed carries prior exact results separately.
                memory = project_agent_memory(run, trace | {"evidence": trace["evidence"][start:]})
                messages = list(request.messages)
                if state["observed_round"] < 0:
                    messages.append(AgentUserMessage(data=memory))
                elif count > state["observed_round"]:
                    messages.append(
                        AgentUserMessage(
                            data={
                                "evidence": trace["evidence"][state["observed_round"] :],
                                "memory": memory["memory"],
                            }
                        )
                    )
                request = AgentModelRequest.model_validate(
                    request.model_dump(mode="json")
                    | {
                        "messages": [item.model_dump(mode="json") for item in messages],
                        "evidence_round": count,
                    }
                )
            return {"request": request.model_dump(mode="json"), "observed_round": count}
        except DomainError as exc:
            return self._stop(state, exc.code)
        except ValidationError:
            return self._stop(state, "AGENT_CONTEXT_LIMIT")

    def checkpoint(self, state: AgentLoopState) -> None:
        """Durable callers save the exact prefix before exposing the next side effect."""

    def _advance(self, state: AgentLoopState, changes: AgentLoopState) -> None:
        updated: AgentLoopState = {**state, **changes}
        self.checkpoint(updated)
        state.update(updated)

    def invoke_model(
        self, request: AgentModelRequest, turn: int
    ) -> AgentModelResponse[AgentDecision[T]]:
        return invoke_agent(
            self.factory,
            self.settings,
            request,
            self.registry,
            self.decision_model,
            execution=self._execution(turn),
            output_projection=self.output_projection,
            on_round=self.pi.accept_model,
            expected_tools=self.declarations,
        )

    def execute_tool(
        self, request: AgentToolCall, step_key: str, *, rejected: bool = False
    ) -> AgentToolFeedback:
        with self.factory.begin() as db:
            self._context(db)
            validate_agent_execution(db, request.run_id, self.seed.stage)
            return execute_agent_tool(
                db,
                self.registry,
                self.seed.agent_name,
                request,
                step_key=step_key,
                rejected=rejected,
            )

    def _declarations(
        self, request: AgentModelRequest, *, replaying: bool
    ) -> tuple[AgentToolDeclaration, ...]:
        with self.factory.begin() as db:
            _, _, actor = self._context(db)
            return agent_tool_declarations(
                available_agent_tools(
                    db, self.registry, request.run_id, request.agent_name, actor, request.stage
                )
            )

    def _tool(self, event: dict, request: AgentModelRequest) -> AgentToolFeedback:
        call = self.pi.next_tool(event)
        execution = self._execution(self.pi.turn)
        # Pairing identifiers from a provider do not grant an action or its idempotency key.
        action_key = "loop-" + digest({"step": execution.step_key, "call": call.call_id})
        tool_request = call.tool_request(request.run_id).model_copy(
            update={"idempotency_key": action_key}
        )
        if event["type"] == "tool_rejected":
            return self.execute_tool(tool_request, execution.step_key, rejected=True)
        return self.execute_tool(tool_request, execution.step_key)

    def _evidence_images(
        self, feedback: AgentToolFeedback, seen: set[UUID]
    ) -> AgentUserMessage | None:
        tool = self.registry.get(feedback.tool_name)
        if (
            feedback.status != "SUCCEEDED"
            or tool is None
            or not issubclass(tool.result_model, RetrieveEvidenceResult)
        ):
            return None
        evidence = tool.result_model.model_validate(feedback.result)
        assert isinstance(evidence, RetrieveEvidenceResult)
        images = [identity for identity in evidence.image_file_ids if identity not in seen]
        if not images:
            return None
        seen.update(images)
        return AgentUserMessage(
            data={"evidence_manifest_id": str(evidence.manifest_id)}, image_file_ids=images
        )

    def run(self, state: AgentLoopState) -> AgentLoopState:
        """Service Pi's events. The SDK chooses the next model or tool callback."""
        request = AgentModelRequest.model_validate(state["request"])
        replaying = state.get("phase") in {"model", "tools"}
        image_messages: list[AgentUserMessage] = []
        seen_images = set(request.image_ids())
        prepared_round: int | None = None
        try:
            with self.pi:
                self.pi.start(request.messages)
                while True:
                    event = self.pi.event()
                    kind = event["type"]
                    if kind == "prepare":
                        if replaying:
                            prepared_round = state["observed_round"]
                            additions: list[AgentUserMessage] = []
                        else:
                            observed = self.observe(state)
                            if observed.get("outcome") is not None:
                                return {**state, **observed}
                            if (
                                state.get("phase") == "tools"
                                and self.complete_after_tools is not None
                            ):
                                completed = self._finish_after_tools({**state, **observed})
                                if completed is not None:
                                    self.pi.stop()
                                    self.checkpoint(completed)
                                    return completed
                            if state["model_turns"] >= MAX_AGENT_LOOP_TURNS:
                                return {**state, **self._stop(state, "AGENT_MODEL_CALL_LIMIT")}
                            prepared_round = observed["observed_round"]
                            additions = image_messages + [
                                AgentUserMessage.model_validate(message)
                                for message in observed["request"]["messages"][
                                    len(request.messages) :
                                ]
                            ]
                        self.declarations = self._declarations(request, replaying=replaying)
                        self.pi.prepare(self.declarations, additions)
                    elif kind == "model" and prepared_round is not None:
                        messages = self.pi.model_messages(event, self.declarations)
                        if replaying:
                            if messages != request.messages:
                                raise PiRuntimeError("Pi changed a frozen model request")
                        else:
                            turn = state["model_turns"] + 1
                            request = AgentModelRequest.model_validate(
                                state["request"]
                                | {
                                    "messages": messages,
                                    "evidence_round": prepared_round,
                                    "operation_key": self._execution(turn).step_key,
                                }
                            )
                            self._advance(
                                state,
                                AgentLoopState(
                                    request=request.model_dump(mode="json"),
                                    phase="model",
                                    response=None,
                                    model_turns=turn,
                                    observed_round=prepared_round,
                                ),
                            )
                        replaying = False
                        prepared_round = None
                        image_messages.clear()
                        self.pi.turn = state["model_turns"]
                        response = self.invoke_model(request, self.pi.turn)
                        saved = response.model_dump(mode="json")
                        if response.status != "TOOL_CALLS":
                            finished: AgentLoopState = {
                                **state,
                                "response": saved,
                                "phase": "finish",
                            }
                            finished.update(self.finish(finished))
                            self.checkpoint(finished)
                            return finished
                        self._advance(state, AgentLoopState(response=saved, phase="tools"))
                    elif kind in {"tool", "tool_rejected"} and state.get("phase") == "tools":
                        feedback = self._tool(event, request)
                        self.pi.accept_tool(feedback)
                        if feedback.error and (
                            feedback.error.code.startswith("AGENT_")
                            or feedback.error.code in _FATAL_TOOL_ERRORS
                        ):
                            return {**state, **self._stop(state, feedback.error.code)}
                        image = self._evidence_images(feedback, seen_images)
                        if image is not None:
                            image_messages.append(image)
                    else:
                        raise PiRuntimeError("Pi runtime returned an out-of-sequence event")
        except (DomainError, GatewayFailure) as exc:
            return {**state, **self._stop(state, exc.code)}
        except ValidationError:
            return {**state, **self._stop(state, "AGENT_CONTEXT_LIMIT")}

    def _authorize_delivery(self, db: Session) -> None:
        self._context(db)
        validate_agent_execution(db, self.seed.run_id, self.seed.stage)

    def _validate_delivery(self, db: Session, result: T | None) -> None:
        if result is not None and self.validate_result is not None:
            self.validate_result(db, result)

    def _finish_after_tools(self, state: AgentLoopState) -> AgentLoopState | None:
        assert self.complete_after_tools is not None
        with self.factory.begin() as db:
            self._authorize_delivery(db)
            result = self.complete_after_tools(db)
            if result is None:
                return None
            self._validate_delivery(db, result)
        return {
            **state,
            "phase": "finish",
            "response": None,
            "outcome": self._outcome(state, status="COMPLETED", result=result),
        }

    def finish(self, state: AgentLoopState) -> AgentLoopState:
        try:
            decision = self.response_model.model_validate_json(
                json.dumps(state["response"]), strict=True
            ).final
            assert decision is not None
            with self.factory.begin() as db:
                self._authorize_delivery(db)
                self._validate_delivery(db, decision.result)
            return {
                "outcome": self._outcome(
                    state,
                    status="COMPLETED" if decision.decision == "COMPLETE" else "HUMAN_REVIEW",
                    result=decision.result,
                    human_request=decision.human_request,
                )
            }
        except DomainError as exc:
            return self._stop(state, exc.code)
        except ValidationError:
            return self._stop(state, "INVALID_RESPONSE")


def run_agent_loop[T: BaseModel](
    factory: sessionmaker[Session],
    settings: Settings,
    request: AgentModelRequest,
    registry: AgentToolRegistry,
    result_model: type[T],
    *,
    parent_run_id: UUID | None = None,
    validate_result: Callable[[Session, T], None] | None = None,
) -> AgentLoopResult[T]:
    """Execute an already active step; return a delivery, human question or safe stop.

    This does not publish an artifact, approve it or complete the owning workflow.
    The caller owns those domain transitions. Durable pause/resume is a separate layer.
    All model communication uses the shared gateway; no unmetered model callback exists.
    """
    request = AgentModelRequest.model_validate(request.model_dump(mode="json"))
    if any(not isinstance(item, AgentUserMessage) for item in request.messages):
        raise DomainError("AGENT_HISTORY_INVALID", "新循环不能注入模型或工具历史", 422)
    request = freeze_agent_skills(request)
    runtime = _AgentRuntime(factory, settings, request, registry, result_model, validate_result)
    runtime.prepare(parent_run_id)
    state = runtime.run(
        AgentLoopState(
            request=request.model_dump(mode="json"),
            observed_round=-1,
            model_turns=0,
            phase="prepare",
        )
    )
    return runtime.outcome_model.model_validate(state["outcome"])
