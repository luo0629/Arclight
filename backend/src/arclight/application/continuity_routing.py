"""Trusted result routing, separate from the Reviewer's advisory model/tool loop."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_loop_store import checked_loop
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.continuity_review_store import (
    REVIEW_RESOURCE,
    require_review_record,
    save_review_contract,
)
from arclight.application.continuity_reviewer import authorize_reviewer
from arclight.application.continuity_reviewer_evidence import (
    reviewer_loop,
    validate_reviewer_delivery,
)
from arclight.application.continuity_reviews import validate_review_contract
from arclight.application.production_versions import bind, checked_snapshot, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelResponse
from arclight.domain.agent_runtime import AgentDecision, AgentLoopResult
from arclight.domain.continuity_reviews import ContinuityReview, ReviewContractRecord, ReviewTarget
from arclight.domain.continuity_routing import ReviewRoute, human_review_key
from arclight.domain.continuity_selection import ADMISSION_RESOURCE, ReviewAdmission
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import Project, VersionBinding, WorkflowRun


def completed_review(db: Session, loop: AgentLoop) -> ContinuityReview:
    """A checkpoint or caller-supplied conclusion cannot replace the committed model delivery."""
    if loop.status != "COMPLETED":
        raise DomainError("REVIEW_NOT_COMPLETED", "原持久审查尚未完成，不能登记权威结论", 409)
    try:
        outcome = AgentLoopResult[ContinuityReview].model_validate(loop.outcome_json)
        if (
            outcome.status != "COMPLETED"
            or outcome.run_id != loop.run_id
            or outcome.loop_id != loop.id
            or outcome.project_id != loop.project_id
            or outcome.agent_name != "continuity_reviewer"
            or outcome.stage != "storyboard"
            or outcome.input_sha256 != loop.input_sha256
            or outcome.result is None
        ):
            raise ValueError("Completed outcome must identify the exact durable loop")
        action = db.scalar(
            select(AgentLoopAction)
            .where(AgentLoopAction.loop_id == loop.id, AgentLoopAction.kind == "MODEL")
            .order_by(AgentLoopAction.created_at.desc(), AgentLoopAction.id.desc())
            .limit(1)
        )
        if action is None or action.result_json is None or action.gateway_call_id is None:
            raise ValueError("Missing committed model result")
        response = AgentModelResponse[AgentDecision[ContinuityReview]].model_validate(
            action.result_json
        )
        if (
            response.status != "FINAL"
            or response.final is None
            or response.final.decision != "COMPLETE"
            or response.final.result != outcome.result
            or action.request_sha256 != digest(action.request_json)
        ):
            raise ValueError("Outcome must equal the original final model receipt")
        return outcome.result
    except (ValidationError, ValueError, TypeError) as exc:
        raise DomainError(
            "REVIEW_DELIVERY_INVALID", "审查结果与原循环或最终模型回执不一致", 409
        ) from exc


def require_latest_review(db: Session, record: ReviewContractRecord) -> None:
    review = record.review
    rows = list(
        db.scalars(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == review.project_id,
                VersionBinding.resource_type == REVIEW_RESOURCE,
                VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                    "candidate_id"
                ].as_string()
                == str(review.target.candidate_id),
            )
            .order_by(
                VersionBinding.snapshot_json["review_contract"]["sequence"].as_integer().desc()
            )
        )
    )
    if any(
        row.snapshot_json["review_contract"]["review"]["target"]
        != review.target.model_dump(mode="json")
        for row in rows
    ):
        raise DomainError("REVIEW_TARGET_AMBIGUOUS", "同一候选存在不同精确审查目标，不能路由", 409)
    if not rows or rows[0].resource_id != review.result_id:
        raise DomainError("REVIEW_RESULT_STALE", "只能路由该候选的最新精确审查", 409)


def read_routed_review(
    db: Session, review_id: UUID, *, lock_run: bool = True
) -> tuple[ReviewContractRecord, ReviewRoute]:
    """Revalidate a durable route for production-event consumption without model I/O."""
    binding = db.scalar(
        select(VersionBinding).where(
            VersionBinding.resource_type == ADMISSION_RESOURCE,
            VersionBinding.resource_id == review_id,
        )
    )
    if binding is None:
        raise DomainError("REVIEW_ROUTE_REQUIRED", "缺少服务端审查路由凭据", 409)
    try:
        snapshot = checked_snapshot(db, binding)
        route = ReviewRoute.model_validate(snapshot["review_route"])
        admission = ReviewAdmission.model_validate(snapshot["review_admission"])
        # Domain human handling also locks this run before the shared project gate.
        # Acquire it before any record/permission read can take that gate.
        db.get(WorkflowRun, route.run_id, populate_existing=True, with_for_update=lock_run)
        record = require_review_record(db, binding.project_id, review_id, route.routed_by)
        require_project(db, binding.project_id, route.routed_by, write=True)
        review = record.review
        loop = db.get(AgentLoop, route.loop_id, populate_existing=True)
        if (
            route.review_id != review_id
            or route.record_sha256 != digest(record.model_dump(mode="json"))
            or route.conclusion != review.conclusion
            or admission.review_id != route.review_id
            or admission.record_sha256 != route.record_sha256
            or admission.requires_human_review != (route.conclusion == "HUMAN_REVIEW")
            or admission.human_review_request_id
            != (
                uuid5(review.project_id, "human-review/1:" + human_review_key(review_id))
                if route.conclusion == "HUMAN_REVIEW"
                else None
            )
            or snapshot["components"] != [v.model_dump(mode="json") for v in review.versions]
            or loop is None
            or loop.run_id != route.run_id
            or loop.actor_id != route.routed_by
            or completed_review(db, loop) != review
        ):
            raise ValueError("Route, admission and immutable review must identify one delivery")
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        raise DomainError("REVIEW_ROUTE_INVALID", "权威审查路由身份、结论或版本无效", 409) from exc
    require_latest_review(db, record)
    validate_review_contract(db, review, route.routed_by)
    return record, route


def route_reviewer_result(
    db: Session, settings: Settings, run_id: UUID, actor: str, target: ReviewTarget
) -> tuple[ReviewRoute, bool]:
    """Route only a completed server journal, in the caller's business transaction.

    This is deliberately absent from the model tool registry and HTTP input schemas.
    Reentry after a crash reads the same loop; it never invokes a model or generator.
    """
    from arclight.application.production_events import begin_serialized, enqueue_source

    target = ReviewTarget.model_validate(target.model_dump(mode="json"))
    begin_serialized(db)
    loop, _ = reviewer_loop(db, run_id, target)
    loop = checked_loop(db, loop.id, actor=actor)
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    assert run is not None
    authorize_reviewer(db, settings, run, actor)
    db.get(Project, loop.project_id, populate_existing=True, with_for_update=True)
    review = completed_review(db, loop)
    validate_reviewer_delivery(db, settings, run, actor, review)
    record, _ = save_review_contract(db, review, actor)
    require_latest_review(db, record)
    existing = find_binding(db, review.project_id, ADMISSION_RESOURCE, review.result_id)
    if existing is not None:
        _, route = read_routed_review(db, review.result_id)
        return route, False
    route = ReviewRoute(
        review_id=review.result_id,
        record_sha256=digest(record.model_dump(mode="json")),
        run_id=run.id,
        loop_id=loop.id,
        conclusion=review.conclusion,
        destination="READY_FOR_REVIEW" if review.conclusion == "PASS" else review.conclusion,
        event_id=uuid5(review.result_id, "production-event:1:REVIEW:1"),
        routed_by=actor,
        routed_at=datetime.now(UTC),
    )
    admission = ReviewAdmission(
        review_id=review.result_id,
        record_sha256=route.record_sha256,
        requires_human_review=review.conclusion == "HUMAN_REVIEW",
        human_review_request_id=(
            uuid5(review.project_id, "human-review/1:" + human_review_key(review.result_id))
            if review.conclusion == "HUMAN_REVIEW"
            else None
        ),
    )
    bind(
        db,
        review.project_id,
        ADMISSION_RESOURCE,
        review.result_id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [v.model_dump(mode="json") for v in review.versions],
            "parameters": {},
            "parameter_sha256": digest({}),
            "review_admission": admission.model_dump(mode="json"),
            "review_route": route.model_dump(mode="json"),
        },
    )
    event = enqueue_source(db, "REVIEW", review.result_id, actor)
    assert event.id == route.event_id
    audit(
        db,
        review.project_id,
        actor,
        "continuity_review.routed",
        review.result_id,
        route.model_dump(mode="json"),
    )
    db.flush()
    return route, True
