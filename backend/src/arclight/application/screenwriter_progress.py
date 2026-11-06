"""Authorized working-output projection and transactional, queued human answers."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import _enabled_actor
from arclight.application.agent_loop_store import submit_agent_decision
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit, require_run
from arclight.application.gateway_text import validate_publication
from arclight.application.screenwriter_creation import (
    PROFESSIONAL_VERSIONS,
    creation_loops,
    operation,
    waiting_for_answer,
)
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.screenwriter_creation import FRAGMENT_VERSION
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.models import Episode, OutboxEvent, WorkflowRun


def read_progress(db: Session, run_id: UUID, actor: str) -> dict:
    run = require_run(db, run_id, actor)
    if run.workflow_version not in PROFESSIONAL_VERSIONS:
        raise DomainError("NOT_FOUND", "此运行没有专业编剧工作稿", 404)
    values = []
    for loop in creation_loops(db, run.id):
        key = operation(loop)
        fragment = key.startswith(FRAGMENT_VERSION + ":scene:")
        outcome = loop.outcome_json or {}
        # Never project the seed, system prompt, provider configuration or tool arguments.
        values.append(
            {
                "loop_id": str(loop.id),
                "kind": "scene" if fragment else "plan",
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
            }
        )
    return {
        "run_id": str(run.id),
        "workflow_version": run.workflow_version,
        "operations": values,
        "can_cancel": waiting_for_answer(db, run),
    }


def queue_answer(
    db: Session, run_id: UUID, loop_id: UUID, actor: str, decision: AgentHumanDecision
) -> WorkflowRun:
    # Preserve the shared gateway -> run lock order used by loop execution.
    from arclight.application.model_gateway import lock_gate

    lock_gate(db)
    run = require_run(db, run_id, actor, lock=True)
    _enabled_actor(db, actor)
    validate_publication(db, run_id)
    loop = db.get(AgentLoop, loop_id)
    question = db.get(AgentLoopQuestion, decision.question_id)
    episode = db.get(Episode, run.episode_id)
    if (
        run.workflow_version not in PROFESSIONAL_VERSIONS
        or loop is None
        or loop.run_id != run.id
        or question is None
        or question.loop_id != loop.id
    ):
        raise DomainError("NOT_FOUND", "当前工作问题不存在", 404)
    assert episode is not None
    if (
        decision.input_sha256 != loop.input_sha256
        or question.input_sha256 != loop.input_sha256
        or scope_for_run(run, episode.project_id).input_sha256 != loop.input_sha256
    ):
        raise DomainError("AGENT_STALE_DECISION", "问题或冻结输入已变化", 409)
    previous = db.scalar(
        select(AgentLoopDecision).where(AgentLoopDecision.question_id == question.id)
    )
    if previous is not None:
        if previous.actor_id != actor or previous.decision_json != decision.model_dump(mode="json"):
            raise DomainError("AGENT_DECISION_CONFLICT", "此问题已有不同人工决定", 409)
        return run
    submit_agent_decision(db, loop.id, actor, decision)
    run.status = "QUEUED"
    run.revision += 1
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(
        db,
        run,
        "screenwriter.answer_queued",
        {"loop_id": str(loop.id), "question_id": str(question.id)},
    )
    return run
