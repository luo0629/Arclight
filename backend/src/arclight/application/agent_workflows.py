"""Shared transaction boundaries for professional Agent workflows and human answers."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_limits import _enabled_actor
from arclight.application.agent_loop_store import submit_agent_decision
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit, require_run
from arclight.application.gateway_text import validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_state import AgentRunState, AgentStateError, terminate_agent_state
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.models import Episode, OutboxEvent, WorkflowEvent, WorkflowRun

logger = logging.getLogger(__name__)


def versions_for_agent(agent_name: str) -> set[str]:
    from arclight.application.screenwriter_creation import PROFESSIONAL_VERSIONS
    from arclight.domain.art_workflow import ART_WORKFLOW_VERSIONS
    from arclight.domain.director_creation import DIRECTOR_VERSIONS

    return {
        "screenwriter": PROFESSIONAL_VERSIONS,
        "director": DIRECTOR_VERSIONS,
        "art_director": set(ART_WORKFLOW_VERSIONS),
    }.get(agent_name, set())


def active_workflow(db: Session, run_id: UUID, revision: int) -> WorkflowRun:
    lock_gate(db)
    run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
    if run is None or run.status != "RUNNING" or run.revision != revision:
        raise DomainError("SCREENWRITER_DELIVERY_STALE", "此执行投递已失效", 409)
    validate_publication(db, run_id)
    return run


def wait_for_agent(
    factory: sessionmaker[Session],
    run_id: UUID,
    revision: int,
    outcome: AgentLoopResult,
    agent_name: str = "screenwriter",
) -> bool:
    if outcome.status == "COMPLETED":
        return False
    if outcome.status == "STOPPED":
        raise DomainError(
            outcome.stop_code or f"{agent_name.upper()}_STOPPED",
            "专业创作已停止，请查看问题或显式重试",
            409,
        )
    with factory.begin() as db:
        run = active_workflow(db, run_id, revision)
        run.pending_review = None
        if outcome.status == "WAITING_EXTERNAL":
            assert outcome.waiting_for is not None
            key = outcome.waiting_for.idempotency_key
            prior = db.scalars(
                select(WorkflowEvent).where(
                    WorkflowEvent.run_id == run.id,
                    WorkflowEvent.event_type == f"{agent_name}.external_wait",
                )
            )
            if not any(event.payload_json.get("action_key") == key for event in prior):
                emit(
                    db,
                    run,
                    f"{agent_name}.external_wait",
                    {
                        "loop_id": str(outcome.loop_id),
                        "action_key": key,
                        "waiting_for": outcome.waiting_for.model_dump(mode="json"),
                    },
                )
            return True
        previous = db.scalars(
            select(WorkflowEvent).where(
                WorkflowEvent.run_id == run.id, WorkflowEvent.event_type == f"{agent_name}.question"
            )
        )
        if not any(
            event.payload_json.get("question_id") == str(outcome.question_id) for event in previous
        ):
            emit(
                db,
                run,
                f"{agent_name}.question",
                {"loop_id": str(outcome.loop_id), "question_id": str(outcome.question_id)},
            )
    return True


def fail_workflow(factory: sessionmaker[Session], run_id: UUID, revision: int, code: str) -> None:
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
        if run is None or run.status not in {"QUEUED", "RUNNING"} or run.revision != revision:
            return
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        stopped = code == "AGENT_HUMAN_STOPPED"
        if run.agent_state_json is not None:
            try:
                state = AgentRunState.model_validate(run.agent_state_json)
                failed_step = next(
                    (
                        step.step_id
                        for step in state.steps
                        if step.status in {"RUNNING", "WAITING_REVIEW"}
                    ),
                    None,
                )
                run.agent_state_json = terminate_agent_state(
                    state,
                    scope_for_run(run, episode.project_id),
                    reason="CANCELLED" if stopped else "EXECUTION_FAILED",
                    step_id=failed_step,
                ).model_dump(mode="json")
            except AgentStateError:
                logger.error("workflow_state_invalid run_id=%s", run_id)
        run.status, run.error_code = ("CANCELLED", None) if stopped else ("FAILED", code)
        run.revision += 1
        emit(db, run, "workflow.cancelled" if stopped else "workflow.failed", {"code": code})


class WorkflowExecutionDeferred(RuntimeError):
    """Keep a broker delivery recoverable while an existing worker lease is alive."""


def creation_loops(db: Session, run_id: UUID, agent_name: str = "screenwriter") -> list[AgentLoop]:
    return list(
        db.scalars(
            select(AgentLoop)
            .where(
                AgentLoop.run_id == run_id,
                AgentLoop.seed_json["agent_name"].as_string() == agent_name,
            )
            .order_by(AgentLoop.created_at, AgentLoop.id)
        )
    )


def workflow_agent_loops(db: Session, run: WorkflowRun, agent_name: str) -> list[AgentLoop]:
    """Include only the server-owned art consultation in a collaborative director's work."""
    from arclight.domain.director_collaboration import OPINION_VERSION
    from arclight.domain.director_creation import COLLABORATIVE_DIRECTOR_VERSIONS

    loops = creation_loops(db, run.id, agent_name)
    if agent_name == "director" and run.workflow_version in COLLABORATIVE_DIRECTOR_VERSIONS:
        loops.extend(
            loop
            for loop in creation_loops(db, run.id, "art_director")
            if loop.seed_json.get("operation_key") == OPINION_VERSION
        )
    return sorted(loops, key=lambda loop: (loop.created_at, str(loop.id)))


def waiting_for_answer(db: Session, run: WorkflowRun, agent_name: str = "screenwriter") -> bool:
    """Cancellation needs no model admission, but cannot interrupt an answered question."""
    versions = versions_for_agent(agent_name)
    if run.workflow_version not in versions or run.status != "RUNNING":
        return False
    for loop in workflow_agent_loops(db, run, agent_name):
        identity = (loop.outcome_json or {}).get("question_id")
        if loop.status != "WAITING_HUMAN" or identity is None:
            continue
        question = db.get(AgentLoopQuestion, UUID(identity))
        if (
            question is not None
            and question.loop_id == loop.id
            and not db.scalar(
                select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == question.id)
            )
        ):
            return True
    return False


def queue_answer(
    db: Session,
    run_id: UUID,
    loop_id: UUID,
    actor: str,
    decision: AgentHumanDecision,
    *,
    agent_name: str = "screenwriter",
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
    versions = versions_for_agent(agent_name)
    if (
        run.workflow_version not in versions
        or loop is None
        or loop.run_id != run.id
        or loop not in workflow_agent_loops(db, run, agent_name)
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
        f"{agent_name}.answer_queued",
        {"loop_id": str(loop.id), "question_id": str(question.id)},
    )
    return run
