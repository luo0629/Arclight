"""A separate graph version: rewrite -> exact script review -> end."""

from collections.abc import Callable
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from arclight.domain.agent_state import (
    AgentRunScope,
    AgentRunState,
    AgentStepId,
    AgentStepStatus,
    context_observation_ids,
    state_from_checkpoint,
    transition_agent_step,
)
from arclight.workflows.production import Artifacts

REWRITE_VERSION = "script_rewrite/1.0"


class RewriteState(TypedDict, total=False):
    run_id: str
    rewrite_input: dict
    target_duration_ms: int
    script_version_id: str
    script_approved: bool
    context_refs: dict
    agent_state: dict


def build_rewrite_graph(
    artifacts: Artifacts,
    checkpointer,
    generate: Callable[[dict], dict],
    *,
    contexts: dict[str, dict] | None = None,
    agent_scope: AgentRunScope | None = None,
    agent_state_seed: AgentRunState | dict | None = None,
    agent_state_updated: Callable[[AgentRunState], None] | None = None,
):
    if (agent_scope is None) != (agent_state_seed is None):
        raise ValueError("Agent state scope and seed must be provided together")
    latest_agent_state = (
        AgentRunState.model_validate(agent_state_seed) if agent_state_seed is not None else None
    )

    def record_step(
        state: RewriteState,
        *,
        step_id: AgentStepId,
        target: AgentStepStatus,
        next_step_id: AgentStepId | None = None,
        next_step_status: AgentStepStatus | None = None,
        draft_id: str | None = None,
        rejected: bool = False,
    ) -> dict:
        nonlocal latest_agent_state
        if agent_scope is None or agent_state_seed is None:
            return {}
        current = state_from_checkpoint(
            state,
            agent_scope,
            latest_agent_state or AgentRunState.model_validate(agent_state_seed),
        )
        updated = transition_agent_step(
            current,
            agent_scope,
            expected_revision=current.revision,
            step_id=step_id,
            target=target,
            next_step_id=next_step_id,
            next_step_status=next_step_status,
            draft_id=UUID(draft_id) if draft_id else None,
            observation_ids=context_observation_ids(state, "rewrite"),
            termination_reason="REVIEW_REJECTED" if rejected else None,
        )
        latest_agent_state = updated
        if agent_state_updated is not None:
            agent_state_updated(updated)
        return {"agent_state": updated.model_dump(mode="json")}

    def step_succeeded(state: RewriteState, step_id: AgentStepId) -> bool:
        if agent_scope is None or agent_state_seed is None:
            return False
        current = state_from_checkpoint(
            state,
            agent_scope,
            latest_agent_state or AgentRunState.model_validate(agent_state_seed),
        )
        return any(step.step_id == step_id and step.status == "SUCCEEDED" for step in current.steps)

    def rewrite(state: RewriteState) -> dict:
        if not step_succeeded(state, "rewrite"):
            record_step(state, step_id="rewrite", target="RUNNING")
        identity, _ = artifacts.script(lambda: generate(state["rewrite_input"]))
        ref = (contexts or {}).get("rewrite", {}).get("trace_ref")
        refs = {"context_refs": {"rewrite": ref}} if ref else {}
        return {
            "script_version_id": identity,
            **refs,
            **record_step(
                state,
                step_id="rewrite",
                target="SUCCEEDED",
                next_step_id="script_review",
                next_step_status="WAITING_REVIEW",
                draft_id=identity,
            ),
        }

    def review(state: RewriteState) -> dict:
        decision = interrupt({"resource_type": "script", "version_id": state["script_version_id"]})
        approved = decision["decision"] == "APPROVE"
        return {
            "script_approved": approved,
            **record_step(
                state,
                step_id="script_review",
                target="SUCCEEDED" if approved else "REJECTED",
                rejected=not approved,
            ),
        }

    graph = StateGraph(RewriteState)
    graph.add_node("rewrite", rewrite)
    graph.add_node("script_review", review)
    graph.add_edge(START, "rewrite")
    graph.add_edge("rewrite", "script_review")
    graph.add_edge("script_review", END)
    return graph.compile(checkpointer=checkpointer)
