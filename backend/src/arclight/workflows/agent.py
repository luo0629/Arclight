"""The Agent's conditional decision loop; application services own all effects."""

from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentLoopState(TypedDict, total=False):
    request: dict
    observed_round: int
    model_turns: int
    response: dict
    outcome: dict


class AgentLoopDriver(Protocol):
    def observe(self, state: AgentLoopState) -> AgentLoopState: ...

    def decide(self, state: AgentLoopState) -> AgentLoopState: ...

    def tools(self, state: AgentLoopState) -> AgentLoopState: ...

    def finish(self, state: AgentLoopState) -> AgentLoopState: ...


def build_agent_graph(driver: AgentLoopDriver):
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
    graph.add_edge("finish", END)
    return graph.compile()
