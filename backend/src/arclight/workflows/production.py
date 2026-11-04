from collections.abc import Callable
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.domain.agent_state import (
    AgentRunScope,
    AgentRunState,
    AgentStepId,
    AgentStepStatus,
    context_observation_ids,
    state_from_checkpoint,
    transition_agent_step,
)
from arclight.providers.text import TextProvider

WORKFLOW_VERSION = "1.0"
DIRECTOR_WORKFLOW_VERSION = "director-1.0"


class ProductionState(TypedDict, total=False):
    run_id: str
    story: str
    style_guide: dict
    story_settings: dict
    story_context: dict
    outline_context: dict
    target_duration_ms: int
    script: dict
    script_version_id: str
    script_approved: bool
    storyboard: dict
    storyboard_version_id: str
    storyboard_approved: bool
    context_refs: dict
    agent_state: dict


class Artifacts:
    """节点持久化由运行服务提供，图只管理状态和审核路由。"""

    def script(self, generate: Callable[[], dict]) -> tuple[str, dict]:
        raise NotImplementedError

    def storyboard(self, script_id: str, generate: Callable[[], dict]) -> tuple[str, dict]:
        raise NotImplementedError


def build_graph(
    provider: TextProvider,
    artifacts: Artifacts,
    checkpointer,
    *,
    director_only: bool = False,
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

    def references(state: ProductionState, stage: str) -> dict:
        ref = (contexts or {}).get(stage, {}).get("trace_ref")
        return {"context_refs": {**state.get("context_refs", {}), stage: ref}} if ref else {}

    def record_step(
        state: ProductionState,
        *,
        step_id: AgentStepId,
        target: AgentStepStatus,
        stage: str,
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
            observation_ids=context_observation_ids(state, stage),
            termination_reason="REVIEW_REJECTED" if rejected else None,
        )
        latest_agent_state = updated
        if agent_state_updated is not None:
            agent_state_updated(updated)
        return {"agent_state": updated.model_dump(mode="json")}

    def step_succeeded(state: ProductionState, step_id: AgentStepId) -> bool:
        if agent_scope is None or agent_state_seed is None:
            return False
        current = state_from_checkpoint(
            state,
            agent_scope,
            latest_agent_state or AgentRunState.model_validate(agent_state_seed),
        )
        return any(step.step_id == step_id and step.status == "SUCCEEDED" for step in current.steps)

    def write_script(state: ProductionState) -> dict:
        if not step_succeeded(state, "script"):
            record_step(state, step_id="script", target="RUNNING", stage="script")

        def generate() -> dict:
            args = (state["story"], state["style_guide"], state["target_duration_ms"])
            # Missing keys on legacy checkpoints stay missing; never load live project facts.
            kwargs: dict[str, Any] = {}
            if "story_settings" in state:
                kwargs["story_settings"] = state["story_settings"]
            if "story_context" in state:
                kwargs["story_context"] = state["story_context"]
            if "outline_context" in state:
                kwargs["outline_context"] = state["outline_context"]
            return provider.script(*args, **kwargs).model_dump()

        version_id, content = artifacts.script(generate)
        return {
            "script": content,
            "script_version_id": version_id,
            **references(state, "script"),
            **record_step(
                state,
                step_id="script",
                target="SUCCEEDED",
                stage="script",
                next_step_id="script_review",
                next_step_status="WAITING_REVIEW",
                draft_id=version_id,
            ),
        }

    def review_script(state: ProductionState) -> dict:
        # interrupt 恢复时节点会重新执行；中断之前不执行有副作用的操作。
        decision = interrupt({"resource_type": "script", "version_id": state["script_version_id"]})
        approved = decision["decision"] == "APPROVE"
        return {
            "script_approved": approved,
            **record_step(
                state,
                step_id="script_review",
                target="SUCCEEDED" if approved else "REJECTED",
                stage="script",
                next_step_id="storyboard" if approved else None,
                next_step_status="RUNNING" if approved else None,
                rejected=not approved,
            ),
        }

    def direct(state: ProductionState) -> dict:
        if not step_succeeded(state, "storyboard"):
            record_step(state, step_id="storyboard", target="RUNNING", stage="storyboard")

        def generate() -> dict:
            script = ScriptContent.model_validate(state["script"])
            content = provider.storyboard(script, state["target_duration_ms"])
            return StoryboardContent.model_validate(content).validate_against(script).model_dump()

        version_id, content = artifacts.storyboard(state["script_version_id"], generate)
        return {
            "storyboard": content,
            "storyboard_version_id": version_id,
            **references(state, "storyboard"),
            **record_step(
                state,
                step_id="storyboard",
                target="SUCCEEDED",
                stage="storyboard",
                next_step_id="storyboard_review",
                next_step_status="WAITING_REVIEW",
                draft_id=version_id,
            ),
        }

    def review_storyboard(state: ProductionState) -> dict:
        decision = interrupt(
            {"resource_type": "storyboard", "version_id": state["storyboard_version_id"]}
        )
        approved = decision["decision"] == "APPROVE"
        return {
            "storyboard_approved": approved,
            **record_step(
                state,
                step_id="storyboard_review",
                target="SUCCEEDED" if approved else "REJECTED",
                stage="storyboard",
                rejected=not approved,
            ),
        }

    graph = StateGraph(ProductionState)
    graph.add_node("director", direct)
    graph.add_node("storyboard_review", review_storyboard)
    if director_only:
        graph.add_edge(START, "director")
    else:
        # 保持 1.0 图的节点名和拓扑，已有 checkpoint 仍可恢复。
        graph.add_node("screenwriter", write_script)
        graph.add_node("script_review", review_script)
        graph.add_edge(START, "screenwriter")
        graph.add_edge("screenwriter", "script_review")
        graph.add_conditional_edges(
            "script_review", lambda s: "director" if s["script_approved"] else END
        )
    graph.add_edge("director", "storyboard_review")
    graph.add_edge("storyboard_review", END)
    return graph.compile(checkpointer=checkpointer)
