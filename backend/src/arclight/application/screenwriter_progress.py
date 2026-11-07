"""Authorized working-output projection and transactional, queued human answers."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_workflows import queue_answer as queue_answer
from arclight.application.commands import require_run
from arclight.application.screenwriter_creation import (
    PROFESSIONAL_VERSIONS,
    creation_loops,
    operation,
    waiting_for_answer,
)
from arclight.core.errors import DomainError
from arclight.domain.screenwriter_creation import FRAGMENT_VERSION
from arclight.domain.screenwriter_revision import (
    PATCH_TOOL,
    REVISION_VERSION,
    CheckPatchResult,
    RevisionSeed,
)
from arclight.persistence.agent_loops import (
    AgentLoopAction,
)


def read_progress(db: Session, run_id: UUID, actor: str) -> dict:
    run = require_run(db, run_id, actor)
    if run.workflow_version not in PROFESSIONAL_VERSIONS:
        raise DomainError("NOT_FOUND", "此运行没有专业编剧工作稿", 404)
    values = []
    for loop in creation_loops(db, run.id):
        key = operation(loop)
        fragment = key.startswith(FRAGMENT_VERSION + ":scene:")
        critique = key.startswith(REVISION_VERSION + ":round:")
        revision = {}
        if critique:
            seed = RevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
            checks = [
                CheckPatchResult.model_validate(action.result_json["result"]).model_dump(
                    mode="json"
                )
                for action in db.scalars(
                    select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)
                )
                if action.kind == "TOOL"
                and action.request_json.get("tool_name") == PATCH_TOOL
                and action.result_json
                and action.result_json.get("status") == "SUCCEEDED"
            ]
            revision = {
                "round": seed.round,
                "remaining_patches": seed.remaining_patches,
                "source_sha256": seed.source_sha256,
                "deterministic_issues": [
                    item.model_dump(mode="json") for item in seed.deterministic_issues
                ],
                "previous_issues": [item.model_dump(mode="json") for item in seed.previous_issues],
                "working_script": seed.source_script.model_dump(mode="json"),
                "patch_checks": checks,
            }
        outcome = loop.outcome_json or {}
        # Never project the seed, system prompt, provider configuration or tool arguments.
        values.append(
            {
                "loop_id": str(loop.id),
                "kind": "scene" if fragment else "critique" if critique else "plan",
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
                **({"revision": revision} if critique else {}),
            }
        )
    return {
        "run_id": str(run.id),
        "workflow_version": run.workflow_version,
        "operations": values,
        "can_cancel": waiting_for_answer(db, run),
    }
