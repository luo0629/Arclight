from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.providers.text import TextProvider

WORKFLOW_VERSION = "1.0"


class ProductionState(TypedDict, total=False):
    run_id: str
    story: str
    style_guide: dict
    target_duration_ms: int
    script: dict
    script_version_id: str
    script_approved: bool
    storyboard: dict
    storyboard_version_id: str
    storyboard_approved: bool


class Artifacts:
    """节点持久化由运行服务提供，图只管理状态和审核路由。"""

    def script(self, generate: Callable[[], dict]) -> tuple[str, dict]:
        raise NotImplementedError

    def storyboard(self, script_id: str, generate: Callable[[], dict]) -> tuple[str, dict]:
        raise NotImplementedError


def build_graph(provider: TextProvider, artifacts: Artifacts, checkpointer):
    def write_script(state: ProductionState) -> dict:
        version_id, content = artifacts.script(
            lambda: provider.script(state["story"], state["style_guide"]).model_dump()
        )
        return {"script": content, "script_version_id": version_id}

    def review_script(state: ProductionState) -> dict:
        # interrupt 恢复时节点会重新执行；中断之前不执行有副作用的操作。
        decision = interrupt({"resource_type": "script", "version_id": state["script_version_id"]})
        return {"script_approved": decision["decision"] == "APPROVE"}

    def direct(state: ProductionState) -> dict:
        def generate() -> dict:
            script = ScriptContent.model_validate(state["script"])
            content = provider.storyboard(script, state["target_duration_ms"])
            return StoryboardContent.model_validate(content).validate_against(script).model_dump()

        version_id, content = artifacts.storyboard(state["script_version_id"], generate)
        return {"storyboard": content, "storyboard_version_id": version_id}

    def review_storyboard(state: ProductionState) -> dict:
        decision = interrupt(
            {"resource_type": "storyboard", "version_id": state["storyboard_version_id"]}
        )
        return {"storyboard_approved": decision["decision"] == "APPROVE"}

    graph = StateGraph(ProductionState)
    graph.add_node("screenwriter", write_script)
    graph.add_node("script_review", review_script)
    graph.add_node("director", direct)
    graph.add_node("storyboard_review", review_storyboard)
    graph.add_edge(START, "screenwriter")
    graph.add_edge("screenwriter", "script_review")
    graph.add_conditional_edges(
        "script_review", lambda s: "director" if s["script_approved"] else END
    )
    graph.add_edge("director", "storyboard_review")
    graph.add_edge("storyboard_review", END)
    return graph.compile(checkpointer=checkpointer)
