"""Continuity handling through the shared human journal, without executing an Agent."""

from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.audit import audit
from arclight.application.commands import require_run
from arclight.application.continuity_reviewer_evidence import reviewer_loop
from arclight.application.continuity_reviews import (
    require_review_target,
    validate_review_references,
)
from arclight.application.continuity_routing import read_routed_review
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.human_reviews import ReviewHandler, request_review, require_review_actor
from arclight.application.model_gateway import utc
from arclight.application.production_versions import bind, checked_snapshot, find_binding
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.continuity_escalations import (
    ESCALATION_RESOURCE,
    INCOMPLETE_REVIEW_HANDLER,
    LIMIT_STOP_CODES,
    ContinuityEscalation,
)
from arclight.domain.continuity_reviewer import ReviewLoopSeed
from arclight.domain.continuity_reviews import ContinuityReview, ReviewContractRecord, ReviewTarget
from arclight.domain.continuity_routing import ReviewRoute, human_review_key
from arclight.domain.continuity_selection import ADMISSION_RESOURCE, HUMAN_REVIEW_HANDLER
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import Project


def escalation_context(
    db: Session, run_id: UUID, target: ReviewTarget, actor: str
) -> tuple[AgentLoop, ReviewLoopSeed]:
    """Authorize handling frozen facts; this grants no new execution or budget."""
    run = require_run(db, run_id, actor, lock=True)
    loop, seed = reviewer_loop(db, run_id, target)
    request = AgentModelRequest.model_validate(loop.seed_json)
    validate_publication(db, run_id)
    require_review_actor(db, loop.project_id, loop.actor_id, owner=False)
    if (
        run.status not in {"RUNNING", "WAITING_REVIEW", "SUCCEEDED"}
        or execution_actor(db, run) != loop.actor_id
        or actor != loop.actor_id
        or loop.project_id != seed.project_id
        or run.episode_id != seed.episode_id
        or request.run_id != run.id
        or request.stage != "storyboard"
        or digest(loop.seed_json) != loop.request_sha256
        or loop.thread_id != f"agent-loop:{loop.id}"
        or scope_for_run(run, seed.project_id).input_sha256 != loop.input_sha256
    ):
        raise DomainError("REVIEW_INPUT_CHANGED", "升级须保留原审查目标、运行及冻结输入", 409)
    validate_review_references(
        db, seed.project_id, seed.episode_id, target, seed.required_references, loop.actor_id
    )
    return loop, seed


def escalation_facts(db: Session, loop: AgentLoop, seed: ReviewLoopSeed) -> ContinuityEscalation:
    outcome = AgentLoopResult[ContinuityReview].model_validate(loop.outcome_json)
    references = seed.required_references
    review_id = None
    reason: Literal["HUMAN_REVIEW", "LIMIT_EXHAUSTED"]
    if loop.status == "COMPLETED":
        record, route = read_routed_review(db, seed.result_id)
        review = record.review
        require_completed_after_escalation(db, record)
        if route.loop_id != loop.id or review.conclusion != "HUMAN_REVIEW":
            raise DomainError("REVIEW_ESCALATION_INVALID", "仅权威人工审查结论可以登记例外", 409)
        reason = "HUMAN_REVIEW"
        reasons = [f"RISK_{review.risk}"] if review.risk in {"HIGH", "CRITICAL"} else []
        if review.confidence.level in {"LOW", "UNKNOWN"}:
            reasons.append("LOW_CONFIDENCE")
        reasons.extend(item.reason for item in review.confidence.uncertainty)
        reasons = sorted(set(reasons)) or ["REVIEWER_ESCALATION"]
        review_id = review.result_id
        input_sha256 = route.record_sha256
        references = review.references
    elif (
        loop.status == "STOPPED"
        and outcome.status == "STOPPED"
        and outcome.stop_code in LIMIT_STOP_CODES
        and outcome.project_id == loop.project_id
        and outcome.agent_name == "continuity_reviewer"
        and outcome.stage == "storyboard"
        and outcome.run_id == loop.run_id
        and outcome.loop_id == loop.id
        and outcome.input_sha256 == loop.input_sha256
    ):
        reason = "LIMIT_EXHAUSTED"
        reasons = [outcome.stop_code]
        input_sha256 = digest(
            {"input": loop.input_sha256, "seed": loop.request_sha256, "outcome": loop.outcome_json}
        )
    else:
        raise DomainError(
            "REVIEW_ESCALATION_INVALID", "当前循环没有可处理的权威结论或耗尽停止", 409
        )
    return ContinuityEscalation(
        project_id=seed.project_id,
        episode_id=seed.episode_id,
        run_id=loop.run_id,
        loop_id=loop.id,
        thread_id=loop.thread_id,
        target=seed.target,
        requested_by=loop.actor_id,
        reason=reason,
        reasons=reasons,
        review_id=review_id,
        input_sha256=input_sha256,
        outcome_sha256=digest(loop.outcome_json),
        source_version_ids=sorted(
            {seed.target.asset_version_id, seed.target.storyboard_id}
            | {ref.resource_id for ref in references if ref.role in {"REFERENCE", "VISUAL_BIBLE"}},
            key=str,
        ),
        stop_code=outcome.stop_code,
    )


def escalation_snapshot(db: Session, pause: ContinuityEscalation) -> ReviewSnapshot:
    version, _, _ = require_review_target(
        db, pause.project_id, pause.episode_id, pause.target, pause.requested_by
    )
    return ReviewSnapshot(
        project_id=pause.project_id,
        episode_id=pause.episode_id,
        run_id=pause.run_id,
        # The shared domain handoff retains the parent workflow thread. The
        # immutable escalation also retains the original reviewer loop/thread.
        thread_id=str(pause.run_id),
        kind="EXCEPTIONAL_SHOT",
        object_id=pause.review_id or pause.loop_id,
        version_id=pause.target.asset_version_id,
        content_sha256=version.checksum,
        input_sha256=pause.input_sha256,
        source_version_ids=pause.source_version_ids,
    )


def read_escalation(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    binding = find_binding(db, frozen.project_id, ESCALATION_RESOURCE, frozen.object_id)
    if binding is None:
        raise DomainError("REVIEW_ESCALATION_REQUIRED", "缺少原审查升级的不可变凭据", 409)
    try:
        value = checked_snapshot(db, binding)
        pause = ContinuityEscalation.model_validate(value["continuity_escalation"])
        loop, seed = escalation_context(db, pause.run_id, pause.target, pause.requested_by)
        if (
            pause != escalation_facts(db, loop, seed)
            or binding.resource_id != (pause.review_id or pause.loop_id)
            or binding.project_id != pause.project_id
            or value["components"] != [item.model_dump(mode="json") for item in seed.versions]
        ):
            raise ValueError("Escalation must identify the same durable facts")
    except (ValidationError, KeyError, ValueError, TypeError) as exc:
        raise DomainError(
            "REVIEW_ESCALATION_INVALID", "升级记录与原审查、原因或精确输入不一致", 409
        ) from exc
    return escalation_snapshot(db, pause)


def request_escalation(
    db: Session, run_id: UUID, actor: str, target: ReviewTarget
) -> HumanReviewRequest:
    """Trusted entry; reads the journal rather than accepting a caller's result."""
    from arclight.application.production_events import begin_serialized

    begin_serialized(db)
    loop, seed = escalation_context(db, run_id, target, actor)
    db.get(Project, seed.project_id, populate_existing=True, with_for_update=True)
    pause = escalation_facts(db, loop, seed)
    identity = pause.review_id or pause.loop_id
    previous = find_binding(db, pause.project_id, ESCALATION_RESOURCE, identity)
    bind(
        db,
        pause.project_id,
        ESCALATION_RESOURCE,
        identity,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [item.model_dump(mode="json") for item in seed.versions],
            "parameters": {},
            "parameter_sha256": digest({}),
            "continuity_escalation": pause.model_dump(mode="json"),
        },
    )
    request = request_review(
        db,
        actor,
        human_review_key(identity) if pause.review_id else f"continuity-limit:{identity}",
        HUMAN_REVIEW_HANDLER if pause.review_id else INCOMPLETE_REVIEW_HANDLER,
        escalation_snapshot(db, pause),
    )
    if previous is None:
        audit(
            db,
            pause.project_id,
            actor,
            "continuity_review.escalated",
            identity,
            {
                "request_id": str(request.id),
                "reasons": pause.reasons,
                "loop_id": str(loop.id),
                "thread_id": loop.thread_id,
            },
        )
    return request


def check_decision(
    db: Session, request: HumanReviewRequest, decision: HumanDecision, actor: str
) -> None:
    if not decision.comment.strip():
        raise DomainError("REVIEW_REASON_REQUIRED", "连续性人工处理须记录例外或拒绝依据", 422)
    if request.handler == INCOMPLETE_REVIEW_HANDLER and decision.decision == "APPROVE":
        raise DomainError(
            "REVIEW_NOT_COMPLETED", "耗尽的审查尚未完成，人工决定不能代替连续性审查", 409
        )


def apply_decision(
    db: Session, request: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    value = HumanDecision.model_validate(decision.decision_json)
    check_decision(db, request, value, actor)
    status = "APPROVED" if value.decision == "APPROVE" else "REJECTED"
    audit(
        db,
        request.project_id,
        actor,
        "continuity_review.human_handled",
        request.id,
        {"decision_id": str(decision.id), "status": status},
    )
    # This handoff handles the domain wait. It never changes the model's
    # conclusion, its checkpoint, asset lifecycle, selection or original limits.
    return {"status": status}


def require_completed_after_escalation(db: Session, record: ReviewContractRecord) -> None:
    """A newer persisted attempt blocks old results before or after its stop."""
    attempts = list(
        db.scalars(
            select(AgentLoop).where(
                AgentLoop.project_id == record.review.project_id,
                AgentLoop.seed_json["agent_name"].as_string() == "continuity_reviewer",
                AgentLoop.seed_json["messages"][0]["data"]["target"]["candidate_id"].as_string()
                == str(record.review.target.candidate_id),
            )
        )
    )
    if not attempts:
        return
    binding = find_binding(
        db, record.review.project_id, ADMISSION_RESOURCE, record.review.result_id
    )
    try:
        route = ReviewRoute.model_validate(
            checked_snapshot(db, binding)["review_route"] if binding else None
        )
        completed = db.get(AgentLoop, route.loop_id, populate_existing=True)
        if (
            completed is None
            or completed.status != "COMPLETED"
            or any(
                loop.id != completed.id and utc(completed.created_at) <= utc(loop.created_at)
                for loop in attempts
            )
        ):
            raise ValueError("A newer completed review is required")
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        raise DomainError(
            "REVIEW_NOT_COMPLETED", "新一轮审查尚未完成权威准入，旧结论不能替代重新审查", 409
        ) from exc


CONTINUITY_HANDLERS = {
    HUMAN_REVIEW_HANDLER: ReviewHandler(read_escalation, apply_decision, check_decision),
    INCOMPLETE_REVIEW_HANDLER: ReviewHandler(read_escalation, apply_decision, check_decision),
}
