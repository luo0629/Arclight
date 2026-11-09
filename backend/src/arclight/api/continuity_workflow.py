"""Start exact candidate reviews asynchronously; conclusions remain server-owned."""

from uuid import UUID

from fastapi import APIRouter, Request

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import AcceptedRun
from arclight.application.agent_workflows import queue_answer
from arclight.application.commands import require_project
from arclight.application.continuity_workflow import read_work, start_candidate_review
from arclight.application.production_versions import find_binding
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.continuity_workflow import REVIEW_BLOCK_RESOURCE, CandidateReviewCreate
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import WorkflowRun

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
    work = read_work(db, project_id, candidate.id)
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
