"""Read server-issued review objects and submit exact, authenticated decisions."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Resource
from arclight.application.commands import require_project
from arclight.application.human_reviews import (
    require_review,
    require_review_authority,
    resume_review,
    review_blocker,
    submit_decision,
)
from arclight.application.short_drama_budget import root_for_workflow
from arclight.domain.human_reviews import HumanDecision, ReviewResume, ReviewSnapshot
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import WorkflowRun

router = APIRouter(prefix="/api/v1", tags=["human-reviews"])


class ReviewResource(Resource):
    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DecisionRead(ReviewResource):
    request_id: UUID
    actor_id: str
    decision_json: HumanDecision


class ResumeRead(ReviewResource):
    request_id: UUID
    decision_id: UUID
    actor_id: str
    result_json: dict


class HumanReviewRead(ReviewResource):
    project_id: UUID
    requested_by: str
    request_sha256: str
    snapshot: ReviewSnapshot
    status: Literal["WAITING_REVIEW", "DECIDED", "RESUMED", "BLOCKED"]
    blocking_code: str | None
    automatic_production_id: UUID | None
    decision: DecisionRead | None
    resume: ResumeRead | None


def serialize(db: Database, row: HumanReviewRequest) -> HumanReviewRead:
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == row.id)
    )
    receipt = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == row.id))
    blocker = review_blocker(db, row) if receipt is None else None
    root = root_for_workflow(db, row.run_id)
    return HumanReviewRead(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        project_id=row.project_id,
        requested_by=row.requested_by,
        request_sha256=row.request_sha256,
        snapshot=ReviewSnapshot.model_validate(row.snapshot_json),
        status="RESUMED"
        if receipt
        else "BLOCKED"
        if blocker
        else "DECIDED"
        if decision
        else "WAITING_REVIEW",
        blocking_code=blocker,
        automatic_production_id=root.id if root is not None else None,
        decision=DecisionRead.model_validate(decision) if decision else None,
        resume=ResumeRead.model_validate(receipt) if receipt else None,
    )


@router.get("/projects/{project_id}/human-reviews")
def list_reviews(
    project_id: UUID,
    db: Database,
    actor: Actor,
    version_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[HumanReviewRead]:
    require_project(db, project_id, actor)
    automatic = (
        select(AgentRunLimit.run_id)
        .join(WorkflowRun, WorkflowRun.id == AgentRunLimit.root_run_id)
        .where(
            AgentRunLimit.run_id == HumanReviewRequest.run_id,
            WorkflowRun.input_json["short_drama"]["script_version_id"].as_string().is_not(None),
        )
        .exists()
    )
    rows = db.scalars(
        select(HumanReviewRequest)
        .where(
            HumanReviewRequest.project_id == project_id,
            *(
                [HumanReviewRequest.snapshot_json["version_id"].as_string() == str(version_id)]
                if version_id is not None
                else [~automatic]
            ),
        )
        .order_by(HumanReviewRequest.created_at.desc(), HumanReviewRequest.id)
        .limit(limit)
        .offset(offset)
    )
    return [serialize(db, row) for row in rows]


@router.get("/human-reviews/{request_id}")
def get_review(request_id: UUID, db: Database, actor: Actor) -> HumanReviewRead:
    return serialize(db, require_review(db, request_id, actor))


def require_generic_review_command(db: Database, request_id: UUID, actor: str) -> None:
    from arclight.application.render_reviews import EXPORT_HANDLER, formal_export_required

    row = require_review(db, request_id, actor)
    if row.handler == EXPORT_HANDLER:
        require_review_authority(db, row, actor, owner=True)
        raise formal_export_required()


@router.post("/human-reviews/{request_id}/decisions")
def decide(request_id: UUID, payload: HumanDecision, db: Database, actor: Actor) -> DecisionRead:
    require_generic_review_command(db, request_id, actor)
    return DecisionRead.model_validate(submit_decision(db, request_id, actor, payload))


@router.post("/human-reviews/{request_id}/resume")
def resume(request_id: UUID, payload: ReviewResume, db: Database, actor: Actor) -> ResumeRead:
    require_generic_review_command(db, request_id, actor)
    return ResumeRead.model_validate(resume_review(db, request_id, actor, payload))
