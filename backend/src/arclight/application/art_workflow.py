"""Exact art delivery projection and transactional handoff from the shared review service."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_tools import grant_agent_tools
from arclight.application.agent_workflows import creation_loops, waiting_for_answer
from arclight.application.art_direction import art_direction_registry
from arclight.application.commands import emit, require_run
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.art_direction import COMPARISON_TOOL
from arclight.domain.art_generation import DELIVERY_KEY, ArtDelivery
from arclight.domain.art_research import RESEARCH_TOOL
from arclight.domain.art_workflow import ART_WORKFLOW_VERSIONS
from arclight.domain.human_reviews import HumanDecision
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.art_proposals import ArtProposalRecord, ArtProposalVersion
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import Episode, OutboxEvent, WorkflowRun


def authorize_art(db: Session, run: WorkflowRun, actor: str, settings: Settings) -> None:
    grant_agent_tools(
        db,
        AgentToolGrantRequest(
            run_id=run.id,
            agent_name="art_director",
            allowed_tools=[RESEARCH_TOOL, COMPARISON_TOOL]
            + (
                ["request_art_concepts", "observe_art_concepts"]
                if run.input_json["art_direction"].get("concept")
                else []
            ),
        ),
        actor,
        art_direction_registry(settings),
    )


def proposal_run(
    db: Session, proposal: ArtProposalRecord, version: ArtProposalVersion
) -> WorkflowRun | None:
    if version.creation_key != proposal.creation_key:
        return None
    loop = db.get(AgentLoop, proposal.creation_key)
    run = db.get(WorkflowRun, loop.run_id) if loop else None
    return run if run and run.workflow_version in ART_WORKFLOW_VERSIONS else None


def delivered(db: Session, run_id: UUID) -> ArtDelivery | None:
    from arclight.application.art_generation import saved_art_delivery

    for loop in creation_loops(db, run_id, "art_director"):
        action = db.scalar(
            select(AgentLoopAction).where(
                AgentLoopAction.loop_id == loop.id, AgentLoopAction.action_key == DELIVERY_KEY
            )
        )
        if action:
            return saved_art_delivery(db, loop, action)
    return None


def queue_art_review(
    db: Session, request: HumanReviewRequest, decision: HumanReviewDecision
) -> None:
    """Called inside the original approval transaction; never approves an asset itself."""
    if request.run_id is None:
        return
    run = db.get(WorkflowRun, request.run_id, populate_existing=True, with_for_update=True)
    if run is None or run.workflow_version not in ART_WORKFLOW_VERSIONS:
        raise DomainError("ART_REVIEW_SCOPE", "美术审核不属于原生成运行", 409)
    delivery = delivered(db, run.id)
    if (
        delivery is None
        or delivery.review_request_id != request.id
        or run.status != "WAITING_REVIEW"
        or run.pending_review != review_target(delivery)
    ):
        raise DomainError("ART_REVIEW_STALE", "生成运行已变化，不能应用此审核", 409)
    run.resume_json = {
        **review_target(delivery),
        "decision_id": str(decision.id),
        "request_sha256": request.request_sha256,
    }
    run.pending_review = None
    run.status = "QUEUED"
    run.revision += 1
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    emit(db, run, "art.review_queued", run.resume_json)


def review_target(delivery: ArtDelivery) -> dict:
    return {
        "resource_type": "art_proposal",
        "version_id": str(delivery.version_id),
        "review_request_id": str(delivery.review_request_id),
    }


def saved_review(db: Session, delivery: ArtDelivery, resume: dict) -> bool:
    """Checkpoint data can only refer to an already committed business decision."""
    request = db.get(HumanReviewRequest, delivery.review_request_id)
    handoff = db.scalar(
        select(HumanReviewResume).where(HumanReviewResume.request_id == delivery.review_request_id)
    )
    decision = db.get(HumanReviewDecision, handoff.decision_id) if handoff else None
    if (
        request is None
        or decision is None
        or resume
        != {
            **review_target(delivery),
            "decision_id": str(decision.id),
            "request_sha256": request.request_sha256,
        }
    ):
        raise DomainError("ART_REVIEW_STALE", "恢复须引用原精确审核及已保存决定", 409)
    value = HumanDecision.model_validate(decision.decision_json)
    if value.version_id != delivery.version_id or value.request_sha256 != request.request_sha256:
        raise DomainError("ART_REVIEW_STALE", "人工决定与原提案不一致", 409)
    return value.decision == "APPROVE"


def read_progress(db: Session, run_id: UUID, actor: str) -> dict:
    run = require_run(db, run_id, actor)
    if run.workflow_version not in ART_WORKFLOW_VERSIONS:
        raise DomainError("NOT_FOUND", "此运行没有美术提案任务", 404)
    operations = []
    for loop in creation_loops(db, run.id, "art_director"):
        outcome = loop.outcome_json or {}
        operations.append(
            {
                "loop_id": str(loop.id),
                "status": loop.status,
                "input_sha256": loop.input_sha256,
                "model_turns": outcome.get("model_turns", 0),
                "question_id": outcome.get("question_id"),
                "human_request": outcome.get("human_request")
                if loop.status == "WAITING_HUMAN"
                else None,
                "waiting_for": outcome.get("waiting_for")
                if loop.status == "WAITING_EXTERNAL"
                else None,
                "stop_code": outcome.get("stop_code"),
                "result": outcome.get("result"),
            }
        )
    from arclight.application.agent_external_waits import waiting_for_external
    from arclight.application.art_concept_jobs import concept_jobs, concept_round

    delivery = delivered(db, run.id)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    return {
        "run_id": str(run.id),
        "project_id": str(episode.project_id),
        "task": run.input_json["art_direction"]["task"],
        "operations": operations,
        "delivery": delivery.model_dump(mode="json") if delivery else None,
        "concept_plan": run.input_json["art_direction"].get("concept"),
        "concept_rounds": [concept_round(db, job) for job in concept_jobs(db, run_id=run.id)],
        "can_cancel": waiting_for_answer(db, run, "art_director") or waiting_for_external(db, run),
    }
