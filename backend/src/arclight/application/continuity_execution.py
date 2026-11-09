"""Execute and resume candidate review work through the shared Agent journal."""

import logging
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_workflows import (
    WorkflowExecutionDeferred,
    active_workflow,
    fail_workflow,
)
from arclight.application.commands import emit
from arclight.application.continuity_escalations import request_escalation
from arclight.application.continuity_feedback import (
    resume_revision_feedback,
    review_revision_feedback,
)
from arclight.application.continuity_reviewer import resume_reviewer, review_candidate
from arclight.application.continuity_routing import read_routed_review, route_reviewer_result
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.production_versions import checked_snapshot, find_binding, text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_state import AgentStateError, restore_agent_state, transition_agent_step
from arclight.domain.continuity_escalations import LIMIT_STOP_CODES
from arclight.domain.continuity_feedback import FEEDBACK_VERSION
from arclight.domain.continuity_reviewer import review_operation
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.continuity_workflow import (
    REVIEW_WORK_RESOURCE,
    REVIEW_WORKFLOW_VERSION,
    CandidateReviewWork,
)
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.models import Episode, VersionBinding, WorkflowRun

logger = logging.getLogger(__name__)


def pause_review(
    factory: sessionmaker[Session], run_id: UUID, revision: int, progress: dict
) -> None:
    with factory.begin() as db:
        run = active_workflow(db, run_id, revision)
        run.status = "RUNNING" if progress.get("question_id") else "WAITING_REVIEW"
        run.pending_review = {"resource_type": "continuity_review", **progress}
        emit(db, run, "workflow.waiting_review", run.pending_review)


def execute_continuity(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> bool:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.workflow_type != "continuity_review":
            return False
    try:
        with factory.begin() as db:
            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            assert run is not None
            if run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return True
            if run.workflow_version != REVIEW_WORKFLOW_VERSION:
                raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "审查工作流版本不兼容", 409)
            validate_publication(db, run.id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            settings = text_settings(db, episode.project_id, run, settings)
            scope = scope_for_run(run, episode.project_id)
            state = restore_agent_state(run.agent_state_json, None, scope)
            if state.steps[0].status == "PENDING":
                state = transition_agent_step(
                    state,
                    scope,
                    expected_revision=state.revision,
                    step_id="storyboard",
                    target="RUNNING",
                )
                run.agent_state_json = state.model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
            run.status, run.pending_review = "RUNNING", None
            actor = execution_actor(db, run)
            limits = AgentRunLimits.model_validate(
                run.input_json["continuity_review"]["agent_limits"]
            )
            work = [
                CandidateReviewWork.model_validate(checked_snapshot(db, row)["continuity_work"])
                for row in db.scalars(
                    select(VersionBinding)
                    .where(
                        VersionBinding.project_id == episode.project_id,
                        VersionBinding.resource_type == REVIEW_WORK_RESOURCE,
                        VersionBinding.snapshot_json["continuity_work"]["run_id"].as_string()
                        == str(run.id),
                    )
                    .order_by(VersionBinding.created_at, VersionBinding.id)
                )
            ]
            waiting = list(
                db.scalars(
                    select(AgentLoop).where(
                        AgentLoop.run_id == run.id, AgentLoop.status == "WAITING_HUMAN"
                    )
                )
            )
        for loop in waiting:
            with factory() as db:
                answered = db.scalar(
                    select(AgentLoopDecision.id)
                    .join(AgentLoopQuestion, AgentLoopQuestion.id == AgentLoopDecision.question_id)
                    .where(AgentLoopQuestion.loop_id == loop.id)
                )
            if not answered:
                pause_review(
                    factory,
                    run_id,
                    revision,
                    {
                        "destination": "HUMAN_QUESTION",
                        "loop_id": str(loop.id),
                        "question_id": (loop.outcome_json or {}).get("question_id"),
                    },
                )
                return True
            if loop.seed_json["operation_key"].startswith(FEEDBACK_VERSION + ":"):
                resume_revision_feedback(factory, settings, loop.id, actor)
            else:
                resume_reviewer(factory, settings, loop.id, actor)
        progress: dict = {"destination": "READY_FOR_REVIEW"}
        for item in work:
            if item.revision_id is not None:
                result = review_revision_feedback(
                    factory, settings, run_id, actor, item.revision_id
                )
                progress = result.model_dump(mode="json") | {"destination": result.status}
                if result.status in {"REVIEWING", "STOPPED"}:
                    break
                continue
            identity = uuid5(run_id, review_operation(item.target, item.rerun))
            with factory() as db:
                already_routed = find_binding(db, scope.project_id, ADMISSION_RESOURCE, identity)
            if already_routed is None:
                outcome = review_candidate(
                    factory, settings, run_id, actor, item.target, rerun=item.rerun, limits=limits
                )
                if outcome.status != "COMPLETED":
                    if outcome.status == "STOPPED" and outcome.stop_code not in LIMIT_STOP_CODES:
                        raise DomainError(outcome.stop_code or "REVIEW_STOPPED", "审查已停止", 409)
                    progress = {
                        "destination": "HUMAN_REVIEW",
                        "loop_id": str(outcome.loop_id),
                        "question_id": str(outcome.question_id) if outcome.question_id else None,
                        "stop_code": outcome.stop_code,
                    }
                    break
            with factory.begin() as db:
                active_workflow(db, run_id, revision)
                if already_routed is not None:
                    _, route = read_routed_review(db, identity)
                else:
                    route, _ = route_reviewer_result(db, settings, run_id, actor, item.target)
                if route.conclusion == "HUMAN_REVIEW":
                    request_escalation(db, run_id, actor, item.target)
                progress = {
                    "destination": route.destination,
                    "candidate_id": str(item.candidate_id),
                    "review_id": str(route.review_id),
                }
        pause_review(factory, run_id, revision, progress)
    except DomainError as error:
        if error.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST"}:
            raise WorkflowExecutionDeferred(error.code) from None
        if error.code != "SCREENWRITER_DELIVERY_STALE":
            fail_workflow(factory, run_id, revision, error.code)
    except AgentStateError:
        fail_workflow(factory, run_id, revision, "RUN_STATE_INVALID")
    except Exception as error:
        logger.error("review_failed run_id=%s error_type=%s", run_id, type(error).__name__)
        fail_workflow(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
    return True
