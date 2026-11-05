"""The Agent's conditional decision loop; application services own all effects."""

from collections.abc import Callable
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentLoopState(TypedDict, total=False):
    request: dict
    observed_round: int
    model_turns: int
    response: dict
    outcome: dict | None


class AgentLoopDriver(Protocol):
    def observe(self, state: AgentLoopState) -> AgentLoopState: ...

    def decide(self, state: AgentLoopState) -> AgentLoopState: ...

    def tools(self, state: AgentLoopState) -> AgentLoopState: ...

    def finish(self, state: AgentLoopState) -> AgentLoopState: ...


def build_agent_graph(
    driver: AgentLoopDriver,
    *,
    checkpointer=None,
    human: Callable[[AgentLoopState], AgentLoopState] | None = None,
):
    graph = StateGraph(AgentLoopState)
    graph.add_node("observe", driver.observe)
    graph.add_node("decide", driver.decide)
    graph.add_node("tools", driver.tools)
    graph.add_node("finish", driver.finish)
    graph.add_edge(START, "observe")
    graph.add_conditional_edges(
        "observe",
        lambda state: "stop" if state.get("outcome") else "decide",
        {"stop": END, "decide": "decide"},
    )

    def after_decision(state: AgentLoopState) -> str:
        if state.get("outcome"):
            return "stop"
        return "tools" if state["response"]["status"] == "TOOL_CALLS" else "finish"

    graph.add_conditional_edges(
        "decide", after_decision, {"stop": END, "tools": "tools", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "tools",
        lambda state: "stop" if state.get("outcome") else "observe",
        {"stop": END, "observe": "observe"},
    )
    if human is None:
        graph.add_edge("finish", END)
    else:

        def human_node(state: AgentLoopState) -> AgentLoopState:
            return human(state)

        graph.add_node("human", human_node)
        graph.add_conditional_edges(
            "finish",
            lambda state: (
                "human" if (state.get("outcome") or {}).get("status") == "HUMAN_REVIEW" else "stop"
            ),
            {"human": "human", "stop": END},
        )
        graph.add_conditional_edges(
            "human",
            lambda state: "stop" if state.get("outcome") else "observe",
            {"stop": END, "observe": "observe"},
        )
    return graph.compile(checkpointer=checkpointer)
