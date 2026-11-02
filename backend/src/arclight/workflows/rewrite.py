"""A separate graph version: rewrite -> exact script review -> end."""

from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from arclight.workflows.production import Artifacts

REWRITE_VERSION = "script_rewrite/1.0"


class RewriteState(TypedDict, total=False):
    run_id: str
    rewrite_input: dict
    target_duration_ms: int
    script_version_id: str
    script_approved: bool
    context_refs: dict


def build_rewrite_graph(
    artifacts: Artifacts,
    checkpointer,
    generate: Callable[[dict], dict],
    *,
    contexts: dict[str, dict] | None = None,
):
    def rewrite(state: RewriteState) -> dict:
        identity, _ = artifacts.script(lambda: generate(state["rewrite_input"]))
        ref = (contexts or {}).get("rewrite", {}).get("trace_ref")
        refs = {"context_refs": {"rewrite": ref}} if ref else {}
        return {"script_version_id": identity, **refs}

    def review(state: RewriteState) -> dict:
        decision = interrupt({"resource_type": "script", "version_id": state["script_version_id"]})
        return {"script_approved": decision["decision"] == "APPROVE"}

    graph = StateGraph(RewriteState)
    graph.add_node("rewrite", rewrite)
    graph.add_node("script_review", review)
    graph.add_edge(START, "rewrite")
    graph.add_edge("rewrite", "script_review")
    graph.add_edge("script_review", END)
    return graph.compile(checkpointer=checkpointer)
