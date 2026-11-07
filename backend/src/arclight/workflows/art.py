"""Proposal and exact review nodes over the shared persistent checkpointer."""

from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class ArtState(TypedDict, total=False):
    run_id: str
    input_sha256: str
    delivery: dict
    approved: bool


def build_art_graph(checkpointer, proposal: Callable[[], dict], review: Callable[[dict], bool]):
    def propose(state: ArtState) -> dict:
        return {"delivery": proposal()}

    def approve(state: ArtState) -> dict:
        delivery = state["delivery"]
        decision = interrupt(
            {
                "resource_type": "art_proposal",
                "version_id": delivery["version_id"],
                "review_request_id": delivery["review_request_id"],
            }
        )
        return {"approved": review(decision)}

    graph = StateGraph(ArtState)
    graph.add_node("art_proposal", propose)
    graph.add_node("art_review", approve)
    graph.add_edge(START, "art_proposal")
    graph.add_edge("art_proposal", "art_review")
    graph.add_edge("art_review", END)
    return graph.compile(checkpointer=checkpointer)
