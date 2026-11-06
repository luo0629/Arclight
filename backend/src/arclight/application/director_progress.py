"""Public director working results and observations, without model seeds or credentials."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import require_run
from arclight.application.screenwriter_creation import creation_loops, operation, waiting_for_answer
from arclight.core.errors import DomainError
from arclight.domain.director_creation import DIRECTOR_VERSIONS, FRAGMENT_VERSION
from arclight.domain.director_revision import (
    CHECK_TOOL,
    REVISION_VERSION,
    WORKING_VERSION,
    DirectorRevisionSeed,
)
from arclight.persistence.agent_loops import AgentLoopAction


def read_progress(db: Session, run_id: UUID, actor: str) -> dict:
    run = require_run(db, run_id, actor)
    if run.workflow_version not in DIRECTOR_VERSIONS:
        raise DomainError("NOT_FOUND", "此运行没有专业导演工作稿", 404)
    values = []
    for loop in creation_loops(db, run.id, "director"):
        key = operation(loop)
        fragment = any(
            key.startswith(version + ":scene:") for version in (FRAGMENT_VERSION, WORKING_VERSION)
        )
        revision = key.startswith(REVISION_VERSION + ":round:")
        outcome = loop.outcome_json or {}
        actions = list(
            db.scalars(
                select(AgentLoopAction)
                .where(AgentLoopAction.loop_id == loop.id)
                .order_by(AgentLoopAction.created_at, AgentLoopAction.id)
            )
        )
        observations = [
            {"tool": action.request_json["tool_name"], "feedback": action.result_json}
            for action in actions
            if action.kind == "TOOL" and action.result_json is not None
        ]
        detail = {}
        if revision:
            seed = DirectorRevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
            chosen = (outcome.get("result") or {}).get("check_sha256")
            patches = []
            for action in actions:
                feedback = action.result_json or {}
                checked = feedback.get("result") or {}
                if action.kind == "TOOL" and action.request_json.get("tool_name") == CHECK_TOOL:
                    patch = action.request_json.get("arguments", {}).get("patch")
                    if patch and feedback.get("status") == "SUCCEEDED":
                        patches.append(
                            {
                                "patch": patch,
                                "check": checked,
                                "selected": checked.get("check_sha256") == chosen,
                            }
                        )
            detail = {
                "director_revision": {
                    "round": seed.round,
                    "remaining_patches": seed.remaining_patches,
                    "source_sha256": seed.source_sha256,
                    "working_storyboard": seed.source.model_dump(mode="json"),
                    "initial_check": seed.checks.model_dump(mode="json"),
                    "patches": patches,
                    "script_scenes": [
                        {
                            "scene_id": scene.scene_id,
                            "title": scene.title,
                            "dialogue": [line.model_dump(mode="json") for line in scene.dialogue],
                        }
                        for scene in seed.script.scenes
                    ],
                }
            }
        values.append(
            {
                "loop_id": str(loop.id),
                "kind": "scene" if fragment else "director_check" if revision else "plan",
                "scene_no": int(key.split(":scene:")[1].split(":")[0]) if fragment else None,
                "status": loop.status,
                "result": outcome.get("result"),
                "stop_code": outcome.get("stop_code"),
                "model_turns": outcome.get("model_turns", 0),
                "question_id": outcome.get("question_id"),
                "input_sha256": loop.input_sha256,
                "human_request": outcome.get("human_request")
                if loop.status == "WAITING_HUMAN"
                else None,
                "observations": observations,
                **detail,
            }
        )
    return {
        "run_id": str(run.id),
        "workflow_version": run.workflow_version,
        "operations": values,
        "can_cancel": waiting_for_answer(db, run, "director"),
    }
