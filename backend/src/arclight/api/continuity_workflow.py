"""Start exact candidate reviews asynchronously; conclusions remain server-owned."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.agent_workflows import queue_answer
from arclight.application.commands import require_project
from arclight.application.continuity_workflow import read_work, start_candidate_review
from arclight.application.production_versions import find_binding
from arclight.application.video_review_work import read_temporal_work
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.continuity_workflow import REVIEW_BLOCK_RESOURCE, CandidateReviewCreate
from arclight.domain.narrative_review import NarrativeReviewCreate
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import Episode, WorkflowRun

router = APIRouter(prefix="/api/v1", tags=["continuity-review"])


def accepted(run: WorkflowRun) -> AcceptedRun:
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.post("/projects/{project_id}/generation-candidates/{candidate_id}/review", status_code=202)
def create(
    project_id: UUID,
    candidate_id: UUID,
    payload: CandidateReviewCreate,
    request: Request,
    db: Database,
    actor: Actor,
) -> AcceptedRun:
    return accepted(
        start_candidate_review(
            db, request.app.state.settings, project_id, candidate_id, actor, payload
        )
    )


@router.get("/projects/{project_id}/generation-candidates/{candidate_id}/review")
def progress(project_id: UUID, candidate_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    candidate = db.get(GenerationCandidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise DomainError("NOT_FOUND", "生成候选不存在", 404)
    work = read_work(db, project_id, candidate.id) or read_temporal_work(
        db, project_id, candidate.id
    )
    if work is None:
        block = find_binding(db, project_id, REVIEW_BLOCK_RESOURCE, candidate.id)
        return {
            "work": None,
            "status": "BLOCKED" if block else "UNBOUND",
            "error_code": block.snapshot_json["blocked_review"]["error_code"] if block else None,
        }
    run = db.get(WorkflowRun, work.run_id)
    assert run is not None
    return {
        "work": work.model_dump(mode="json"),
        "status": run.status,
        "error_code": run.error_code,
        "pending_review": run.pending_review,
    }


@router.post("/runs/{run_id}/continuity/{loop_id}/answers", status_code=202)
def answer(
    run_id: UUID,
    loop_id: UUID,
    payload: AgentHumanDecision,
    db: Database,
    actor: Actor,
) -> AcceptedRun:
    return accepted(
        queue_answer(db, run_id, loop_id, actor, payload, agent_name="continuity_reviewer")
    )


@router.post("/projects/{project_id}/narrative-reviews", status_code=202)
def create_narrative_review(
    project_id: UUID, payload: NarrativeReviewCreate, request: Request, db: Database, actor: Actor
) -> AcceptedRun:
    from arclight.application.narrative_review import start_review

    return accepted(start_review(db, request.app.state.settings, project_id, actor, payload))


@router.get("/projects/{project_id}/narrative-reviews")
def narrative_history(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    from arclight.application.narrative_review import report_identity
    from arclight.domain.narrative_review import NARRATIVE_REPORT_RESOURCE, NARRATIVE_REVIEW_VERSION

    require_project(db, project_id, actor)
    runs = db.scalars(
        select(WorkflowRun)
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(
            Episode.project_id == project_id,
            WorkflowRun.workflow_type == "continuity_review",
            WorkflowRun.workflow_version == NARRATIVE_REVIEW_VERSION,
        )
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
        .limit(limit)
        .offset(offset)
    )
    return [
        {
            "run_id": run.id,
            "status": run.status,
            "scope": run.input_json["narrative_review"]["scope"],
            "report_id": report_identity(run.id)
            if find_binding(db, project_id, NARRATIVE_REPORT_RESOURCE, report_identity(run.id))
            else None,
            "created_at": run.created_at,
        }
        for run in runs
    ]


@router.get("/projects/{project_id}/narrative-reviews/{run_id}")
def narrative_progress(project_id: UUID, run_id: UUID, db: Database, actor: Actor) -> dict:
    from arclight.api.human_reviews import serialize
    from arclight.application.narrative_review import (
        current_work,
        read_report,
        read_work,
        report_identity,
    )
    from arclight.domain.narrative_review import NARRATIVE_REPORT_RESOURCE
    from arclight.persistence.human_reviews import HumanReviewRequest

    require_project(db, project_id, actor)
    work = read_work(db, project_id, run_id)
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    report_id = report_identity(run_id)
    report = (
        read_report(db, project_id, report_id)
        if find_binding(db, project_id, NARRATIVE_REPORT_RESOURCE, report_id)
        else None
    )
    human = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.project_id == project_id,
            HumanReviewRequest.creation_key == "narrative:" + str(report_id),
        )
    )
    blocking_code = None
    try:
        current_work(db, project_id, run_id)
    except DomainError as error:
        blocking_code = error.code
    from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion

    question = None
    loop = db.scalar(
        select(AgentLoop).where(AgentLoop.run_id == run_id, AgentLoop.status == "WAITING_HUMAN")
    )
    question_id = (loop.outcome_json or {}).get("question_id") if loop else None
    if loop is not None and question_id:
        row = db.get(AgentLoopQuestion, UUID(question_id))
        if (
            row is not None
            and row.loop_id == loop.id
            and db.scalar(
                select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == row.id)
            )
            is None
        ):
            question = {
                "loop_id": loop.id,
                "question_id": row.id,
                "input_sha256": row.input_sha256,
                "request": row.question_json,
            }
    return {
        "work": work,
        "report": report,
        "status": run.status,
        "error_code": run.error_code,
        "blocking_code": blocking_code,
        "human_review": serialize(db, human) if human else None,
        "question": question,
    }
