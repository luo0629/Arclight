"""Execute narrative QA through the existing persistent Reviewer and gateway."""

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.agent_workflows import (
    WorkflowExecutionDeferred,
    WorkflowRecoveryFailed,
    active_workflow,
    fail_workflow,
    wait_for_agent,
)
from arclight.application.commands import emit
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.human_reviews import request_review
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import run_settings
from arclight.application.narrative_review import current_work, receipt, report_identity
from arclight.application.narrative_review_evidence import validate_assessment
from arclight.application.narrative_review_human import snapshot
from arclight.application.production_versions import bind, find_binding, text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_state import AgentStateError, restore_agent_state, transition_agent_step
from arclight.domain.continuity_escalations import LIMIT_STOP_CODES
from arclight.domain.narrative_review import (
    NARRATIVE_HUMAN_HANDLER,
    NARRATIVE_REPORT_RESOURCE,
    NARRATIVE_REVIEW_PROMPT,
    NARRATIVE_REVIEW_VERSION,
    NarrativeAssessment,
    NarrativeReport,
    NarrativeReviewWork,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.agent_model import agent_transport

logger = logging.getLogger(__name__)


def assess(
    factory: sessionmaker[Session], settings: Settings, work: NarrativeReviewWork, revision: int
) -> tuple[NarrativeAssessment | None, str | None, bool]:
    if agent_transport(settings) is None:
        return None, "当前模型未配置 Reviewer Agent 协议，须人工完成六类叙事检查。", False
    request = AgentModelRequest(
        run_id=work.run_id,
        agent_name="continuity_reviewer",
        stage="storyboard",
        operation_key=NARRATIVE_REVIEW_VERSION,
        agent_version=NARRATIVE_REVIEW_VERSION,
        prompt_version=NARRATIVE_REVIEW_VERSION,
        result_schema_version="narrative-assessment/1",
        system_instruction=NARRATIVE_REVIEW_PROMPT,
        messages=[AgentUserMessage(data={"narrative_work": work.model_dump(mode="json")})],
    )

    def validate(db: Session, result: NarrativeAssessment) -> None:
        current_work(db, work.project_id, work.run_id)
        validate_assessment(work, result)

    with factory() as db:
        loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == work.run_id))
        answered = bool(
            loop
            and db.scalar(
                select(AgentLoopDecision.id)
                .join(AgentLoopQuestion, AgentLoopQuestion.id == AgentLoopDecision.question_id)
                .where(AgentLoopQuestion.loop_id == loop.id)
            )
        )
    try:
        if loop is not None and loop.status == "WAITING_HUMAN":
            if not answered:
                return None, None, True
            result = resume_agent_loop(
                factory,
                settings,
                loop.id,
                loop.actor_id,
                AgentToolRegistry(),
                NarrativeAssessment,
                validate_result=validate,
            )
        else:
            result = run_persistent_agent_loop(
                factory,
                settings,
                request,
                AgentToolRegistry(),
                NarrativeAssessment,
                validate_result=validate,
            )
    except DomainError as error:
        if error.code not in LIMIT_STOP_CODES | {
            "GATEWAY_CAPABILITY_REJECTED",
            "NARRATIVE_REVIEW_CITATION_INVALID",
        }:
            raise
        return None, f"未取得有效模型证据（{error.code}），须人工调查。", False
    if result.status == "COMPLETED" and result.result is not None:
        return result.result, None, False
    if result.status == "STOPPED":
        if result.stop_code not in LIMIT_STOP_CODES:
            raise DomainError(result.stop_code or "NARRATIVE_REVIEW_STOPPED", "叙事审查停止", 409)
        return None, f"审查达到运行限制（{result.stop_code}），须人工调查。", False
    wait_for_agent(factory, work.run_id, revision, result, "continuity_reviewer")
    if result.status == "HUMAN_REVIEW":
        with factory.begin() as db:
            run = active_workflow(db, work.run_id, revision)
            run.pending_review = {
                "resource_type": "narrative_review",
                "destination": "HUMAN_QUESTION",
                "loop_id": str(result.loop_id),
                "question_id": str(result.question_id),
            }
            emit(db, run, "workflow.waiting_review", run.pending_review)
    return None, None, True


def execute_narrative_review(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> None:
    try:
        with factory.begin() as db:
            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            if run is None or run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return
            validate_publication(db, run_id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            project_id = episode.project_id
            lock_dependency_graph(db, project_id)
            work = current_work(db, project_id, run.id)
            settings = run_settings(
                text_settings(db, project_id, run, settings),
                run.input_json.get("_text_service"),
                db,
            )
            state = restore_agent_state(run.agent_state_json, None, scope_for_run(run, project_id))
            if state.steps[0].status == "PENDING":
                run.agent_state_json = transition_agent_step(
                    state,
                    scope_for_run(run, project_id),
                    expected_revision=state.revision,
                    step_id="storyboard",
                    target="RUNNING",
                ).model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
                run.pending_review = None
            # A duplicate/recovered delivery must preserve the unanswered-question
            # marker, otherwise dispatch_outbox would keep requeueing this run.
            run.status = "RUNNING"
        assessment, gap, waiting = assess(factory, settings, work, revision)
        if waiting:
            return
        with factory.begin() as db:
            run = active_workflow(db, run_id, revision)
            lock_dependency_graph(db, project_id)
            current_work(db, project_id, run_id)
            actor = execution_actor(db, run)
            report_id = report_identity(run_id)
            if find_binding(db, project_id, NARRATIVE_REPORT_RESOURCE, report_id) is not None:
                return
            limitations = [*work.limitations, *([gap] if gap else [])]
            checks = assessment.checks if assessment else []
            conclusion: Literal["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"] = (
                "HUMAN_REVIEW"
                if limitations
                or not checks
                or any(check.status == "INSUFFICIENT" for check in checks)
                else "REVISION_REQUIRED"
                if any(check.issues for check in checks)
                else "PASS"
            )
            report = NarrativeReport(
                report_id=report_id,
                run_id=run.id,
                project_id=project_id,
                scope=work.request.scope,
                input_sha256=digest(work.model_dump(mode="json")),
                conclusion=conclusion,
                assessment=assessment,
                limitations=limitations,
                previous_report_id=work.request.previous_report_id,
                recorded_at=datetime.now(UTC),
            )
            bind(
                db,
                project_id,
                NARRATIVE_REPORT_RESOURCE,
                report_id,
                receipt(
                    narrative_report=report.model_dump(mode="json"),
                    report_sha256=digest(report.model_dump(mode="json")),
                ),
            )
            human = request_review(
                db,
                actor,
                "narrative:" + str(report_id),
                NARRATIVE_HUMAN_HANDLER,
                snapshot(run, work, report),
            )
            state = restore_agent_state(run.agent_state_json, None, scope_for_run(run, project_id))
            run.agent_state_json = transition_agent_step(
                state,
                scope_for_run(run, project_id),
                expected_revision=state.revision,
                step_id="storyboard",
                target="WAITING_REVIEW",
            ).model_dump(mode="json")
            run.status = "WAITING_REVIEW"
            run.pending_review = {
                "resource_type": "narrative_review",
                "report_id": str(report_id),
                "human_review_request_id": str(human.id),
                "conclusion": conclusion,
            }
            emit(db, run, "workflow.waiting_review", run.pending_review)
    except DomainError as error:
        if error.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST"}:
            raise WorkflowExecutionDeferred(error.code) from None
        if error.code != "SCREENWRITER_DELIVERY_STALE":
            fail_workflow(factory, run_id, revision, error.code)
    except AgentStateError:
        fail_workflow(factory, run_id, revision, "RUN_STATE_INVALID")
    except WorkflowRecoveryFailed:
        raise
    except Exception as error:
        logger.error(
            "narrative_review_failed run_id=%s error_type=%s", run_id, type(error).__name__
        )
        fail_workflow(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
