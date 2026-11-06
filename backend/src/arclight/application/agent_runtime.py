"""Run the real Agent graph using the existing gateway, tools, evidence and limits."""

import json
from collections.abc import Callable
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_limits import configure_agent_run, validate_agent_execution
from arclight.application.agent_memory import project_agent_memory
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolRegistry, execute_agent_tool
from arclight.application.context_evidence import read_trace
from arclight.application.gateway_agent import invoke_agent
from arclight.application.gateway_text import execution_actor
from arclight.application.model_gateway import lock_gate
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext, AgentRunLimits
from arclight.domain.agent_model import (
    AgentAssistantMessage,
    AgentModelRequest,
    AgentModelResponse,
    AgentToolMessage,
    AgentUserMessage,
)
from arclight.domain.agent_runtime import AgentDecision, AgentLoopResult
from arclight.domain.agent_state import state_for_scope, transition_agent_step
from arclight.domain.agent_tools import AgentToolCall, AgentToolFeedback, RetrieveEvidenceResult
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import Episode, WorkflowRun
from arclight.workflows.agent import AgentLoopState, build_agent_graph

_FATAL_TOOL_ERRORS = frozenset(
    {
        "USER_NOT_AUTHORIZED",
        "UNKNOWN_TOOL",
        "AGENT_NOT_ALLOWED",
        "WORKFLOW_NOT_ACTIVE",
        "RUN_STATE_INVALID",
        "INVOCATION_RECORD_INVALID",
        "IDEMPOTENCY_CONFLICT",
        "REFERENCE_LIMIT",
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
    ):
        self.factory, self.settings, self.seed = factory, settings, seed
        self.registry, self.result_model = registry, result_model
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

    def prepare(self, limits: AgentRunLimits | None, parent_run_id: UUID | None) -> None:
        self._execution(1)  # Validate the server-owned stage before configuring a run.
        with self.factory.begin() as db:
            run, self.project_id, actor = self._context(db)
            previous = db.get(AgentRunLimit, run.id)
            configure_agent_run(
                db,
                run.id,
                actor,
                limits
                or (AgentRunLimits.model_validate(previous.limits_json) if previous else None),
                parent_run_id=parent_run_id
                if parent_run_id is not None
                else (previous.parent_run_id if previous else None),
            )
            self.limits = validate_agent_execution(db, run.id, self.seed.stage)

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
                memory = project_agent_memory(run, trace)
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

    def decide(self, state: AgentLoopState) -> AgentLoopState:
        turn = state["model_turns"] + 1
        advanced: AgentLoopState = {**state, "model_turns": turn}
        try:
            request = AgentModelRequest.model_validate(
                state["request"]
                | {
                    "operation_key": f"loop:{self.identity}:{turn}",
                }
            )
            response = self.invoke_model(request, turn)
            return {"model_turns": turn, "response": response.model_dump(mode="json")}
        except (DomainError, GatewayFailure) as exc:
            return {"model_turns": turn, **self._stop(advanced, exc.code)}

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
        )

    def execute_tool(self, request: AgentToolCall, step_key: str) -> AgentToolFeedback:
        with self.factory.begin() as db:
            self._context(db)
            validate_agent_execution(db, request.run_id, self.seed.stage)
            return execute_agent_tool(
                db, self.registry, self.seed.agent_name, request, step_key=step_key
            )

    def tools(self, state: AgentLoopState) -> AgentLoopState:
        try:
            request = AgentModelRequest.model_validate(state["request"])
            response = self.response_model.model_validate(state["response"])
            messages = [*request.messages, AgentAssistantMessage(tool_calls=response.tool_calls)]
            image_messages: list[AgentUserMessage] = []
            seen_images = set(request.image_ids())
            execution = self._execution(state["model_turns"])
            for call in response.tool_calls:
                # Provider IDs are transport pairing IDs, not authority or durable action keys.
                action_key = "loop-" + digest({"step": execution.step_key, "call": call.call_id})
                tool_request = call.tool_request(request.run_id).model_copy(
                    update={"idempotency_key": action_key}
                )
                feedback = self.execute_tool(tool_request, execution.step_key)
                if feedback.error and (
                    feedback.error.code.startswith("AGENT_")
                    or feedback.error.code in _FATAL_TOOL_ERRORS
                ):
                    return self._stop(state, feedback.error.code)
                messages.append(
                    AgentToolMessage(
                        call_id=call.call_id,
                        feedback=feedback.model_copy(update={"idempotency_key": call.call_id}),
                    )
                )
                tool = self.registry.get(call.tool_name)
                if (
                    feedback.status == "SUCCEEDED"
                    and tool is not None
                    and issubclass(tool.result_model, RetrieveEvidenceResult)
                ):
                    evidence = tool.result_model.model_validate(feedback.result)
                    assert isinstance(evidence, RetrieveEvidenceResult)
                    images = [
                        identity
                        for identity in evidence.image_file_ids
                        if identity not in seen_images
                    ]
                    if images:
                        image_messages.append(
                            AgentUserMessage(
                                data={"evidence_manifest_id": str(evidence.manifest_id)},
                                image_file_ids=images,
                            )
                        )
                        seen_images.update(images)
            updated = AgentModelRequest.model_validate(
                request.model_dump(mode="json")
                | {
                    "messages": [
                        item.model_dump(mode="json") for item in [*messages, *image_messages]
                    ],
                }
            )
            return {"request": updated.model_dump(mode="json")}
        except DomainError as exc:
            return self._stop(state, exc.code)
        except ValidationError:
            return self._stop(state, "AGENT_CONTEXT_LIMIT")

    def finish(self, state: AgentLoopState) -> AgentLoopState:
        try:
            decision = self.response_model.model_validate_json(
                json.dumps(state["response"]), strict=True
            ).final
            assert decision is not None
            with self.factory.begin() as db:
                self._context(db)
                validate_agent_execution(db, self.seed.run_id, self.seed.stage)
                if decision.result is not None and self.validate_result is not None:
                    self.validate_result(db, decision.result)
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
    limits: AgentRunLimits | None = None,
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
    runtime = _AgentRuntime(factory, settings, request, registry, result_model, validate_result)
    runtime.prepare(limits, parent_run_id)
    graph = build_agent_graph(runtime)
    state = graph.invoke(
        AgentLoopState(request=request.model_dump(mode="json"), observed_round=-1, model_turns=0),
        {"recursion_limit": min(runtime.limits.max_steps, runtime.limits.max_model_calls) * 4 + 10},
    )
    return runtime.outcome_model.model_validate(state["outcome"])
