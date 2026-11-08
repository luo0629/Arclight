"""Budget limits, warnings, and owner-reviewed adjustment requests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, field_serializer, field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Resource
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_budget import enforce_budget, policy_for, quote_job, usage
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import (
    BudgetAdjustmentInput,
    BudgetBatchInput,
    BudgetDecisionInput,
    BudgetPolicyInput,
    BudgetSettlementInput,
)
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.generation import GenerationAttempt
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.generation_budget import (
    GenerationBudgetAdjustment,
    GenerationBudgetBatch,
    GenerationBudgetDecision,
    GenerationBudgetQuote,
)
from arclight.persistence.models import GenerationJob

router = APIRouter(prefix="/api/v1/projects/{project_id}/budget", tags=["generation-budget"])


class BudgetRead(BaseModel):
    project_limit: Decimal
    actual_cost: Decimal
    pending_estimate: Decimal
    projected_cost: Decimal
    warning: bool
    warning_percent: int
    policy_revision: int
    task_limit: Decimal | None
    episode_limit: Decimal | None
    batch_limit: Decimal | None
    max_candidates_per_target: int
    currency: str

    @field_serializer(
        "project_limit",
        "actual_cost",
        "pending_estimate",
        "projected_cost",
        "task_limit",
        "episode_limit",
        "batch_limit",
    )
    def money(self, value: Decimal | None) -> str | None:
        return format(value, ".6f") if value is not None else None


class AdjustmentRead(Resource):
    project_id: UUID
    creation_key: UUID
    requested_limit: Decimal
    reason: str
    created_by: str
    approved: bool | None = None
    decision_reason: str | None = None

    @field_serializer("requested_limit")
    def money(self, value: Decimal) -> str:
        return format(value, ".2f")

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_dates(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def read_adjustment(db: Database, row: GenerationBudgetAdjustment) -> AdjustmentRead:
    result = AdjustmentRead.model_validate(row)
    decision = db.scalar(
        select(GenerationBudgetDecision).where(GenerationBudgetDecision.adjustment_id == row.id)
    )
    if decision is not None:
        result.approved = decision.approved
        result.decision_reason = decision.reason
    return result


@router.get("")
def read_budget(project_id: UUID, db: Database, actor: Actor) -> BudgetRead:
    require_project(db, project_id, actor)
    return BudgetRead.model_validate(usage(db, project_id))


@router.patch("")
def update_budget_policy(
    project_id: UUID, payload: BudgetPolicyInput, db: Database, actor: Actor
) -> BudgetRead:
    project = require_project(db, project_id, actor, write=True, owner=True)
    if payload.project_limit < (usage(db, project_id)["actual_cost"]):
        raise DomainError("BUDGET_BELOW_USAGE", "新额度不能低于已发生费用", 409)
    policy = policy_for(db, project_id, lock=True)
    if policy.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "预算策略已变化，请刷新后重试", 409)
    previous_limit = project.budget_limit
    project.budget_limit = payload.project_limit
    policy.task_limit = payload.task_limit
    policy.episode_limit = payload.episode_limit
    policy.batch_limit = payload.batch_limit
    policy.warning_percent = payload.warning_percent
    policy.max_candidates_per_target = payload.max_candidates_per_target
    policy.revision += 1
    audit(
        db,
        project_id,
        actor,
        "budget.policy_updated",
        project_id,
        {
            "revision": policy.revision,
            "previous_limit": str(previous_limit),
            "policy": payload.model_dump(mode="json"),
        },
    )
    db.flush()
    return BudgetRead.model_validate(usage(db, project_id))


@router.post("/batches", status_code=201)
def create_budget_batch(
    project_id: UUID, payload: BudgetBatchInput, db: Database, actor: Actor
) -> dict:
    require_project(db, project_id, actor, write=True)
    existing = db.scalar(
        select(GenerationBudgetBatch).where(
            GenerationBudgetBatch.project_id == project_id,
            GenerationBudgetBatch.creation_key == payload.creation_key,
        )
    )
    if existing is not None:
        if (
            existing.budget_limit != payload.budget_limit
            or existing.episode_id != payload.episode_id
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一批次键不能用于不同预算", 409)
        return {
            "id": existing.id,
            "budget_limit": format(existing.budget_limit, ".6f"),
            "episode_id": existing.episode_id,
        }
    if payload.episode_id is not None:
        from arclight.persistence.models import Episode

        episode = db.get(Episode, payload.episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "预算批次剧集不存在", 404)
        if episode.status == "ARCHIVED":
            raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    policy = policy_for(db, project_id)
    if policy.batch_limit is not None and payload.budget_limit > policy.batch_limit:
        raise DomainError("BUDGET_BATCH_EXCEEDED", "批次额度超过项目批次上限", 409)
    row = GenerationBudgetBatch(
        project_id=project_id,
        episode_id=payload.episode_id,
        creation_key=payload.creation_key,
        budget_limit=payload.budget_limit,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "budget.batch_created",
        row.id,
        {"budget_limit": str(row.budget_limit)},
    )
    return {
        "id": row.id,
        "budget_limit": format(row.budget_limit, ".6f"),
        "episode_id": row.episode_id,
    }


@router.post("/adjustments", status_code=201)
def request_adjustment(
    project_id: UUID, payload: BudgetAdjustmentInput, db: Database, actor: Actor
) -> AdjustmentRead:
    require_project(db, project_id, actor, write=True)
    existing = db.scalar(
        select(GenerationBudgetAdjustment).where(
            GenerationBudgetAdjustment.project_id == project_id,
            GenerationBudgetAdjustment.creation_key == payload.creation_key,
        )
    )
    if existing is not None:
        if existing.requested_limit != payload.requested_limit or existing.reason != payload.reason:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一申请键不能用于不同内容", 409)
        return read_adjustment(db, existing)
    project = require_project(db, project_id, actor)
    if payload.requested_limit <= project.budget_limit:
        raise DomainError("BUDGET_ADJUSTMENT_NOT_INCREASE", "申请额度必须高于当前额度", 422)
    row = GenerationBudgetAdjustment(
        project_id=project_id,
        creation_key=payload.creation_key,
        requested_limit=payload.requested_limit,
        reason=payload.reason,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "budget.adjustment_requested",
        row.id,
        {"requested_limit": str(row.requested_limit)},
    )
    return read_adjustment(db, row)


@router.post("/adjustments/{adjustment_id}/decision")
def decide_adjustment(
    project_id: UUID, adjustment_id: UUID, payload: BudgetDecisionInput, db: Database, actor: Actor
) -> dict:
    project = require_project(db, project_id, actor, write=True, owner=True)
    adjustment = db.get(GenerationBudgetAdjustment, adjustment_id)
    if adjustment is None or adjustment.project_id != project_id:
        raise DomainError("NOT_FOUND", "预算调整申请不存在", 404)
    previous = db.scalar(
        select(GenerationBudgetDecision).where(
            GenerationBudgetDecision.adjustment_id == adjustment.id
        )
    )
    if previous is not None:
        if previous.approved != payload.approved or previous.reason != payload.reason:
            raise DomainError("BUDGET_DECIDED", "预算调整申请已经处理", 409)
        return {
            "id": adjustment.id,
            "approved": previous.approved,
            "project_limit": project.budget_limit,
        }
    policy = policy_for(db, project_id, lock=True)
    if policy.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "预算策略已变化，请刷新后重试", 409)
    if payload.approved:
        if adjustment.requested_limit <= project.budget_limit:
            raise DomainError("BUDGET_ADJUSTMENT_STALE", "当前额度已提升，请重新申请", 409)
        if adjustment.requested_limit < usage(db, project_id)["actual_cost"]:
            raise DomainError("BUDGET_BELOW_USAGE", "批准额度不能低于已发生费用", 409)
        project.budget_limit = adjustment.requested_limit
        policy.revision += 1
    decision = GenerationBudgetDecision(
        adjustment_id=adjustment.id,
        approved=payload.approved,
        reason=payload.reason,
        decided_by=actor,
        policy_revision=policy.revision,
    )
    db.add(decision)
    audit(
        db,
        project_id,
        actor,
        "budget.adjustment_decided",
        adjustment.id,
        {"approved": payload.approved},
    )
    db.flush()
    return {
        "id": adjustment.id,
        "approved": payload.approved,
        "project_limit": project.budget_limit,
    }


@router.get("/adjustments")
def list_adjustments(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[AdjustmentRead]:
    require_project(db, project_id, actor)
    rows = db.scalars(
        select(GenerationBudgetAdjustment)
        .where(GenerationBudgetAdjustment.project_id == project_id)
        .order_by(GenerationBudgetAdjustment.created_at.desc(), GenerationBudgetAdjustment.id)
        .limit(limit)
        .offset(offset)
    )
    return [read_adjustment(db, row) for row in rows]


@router.post("/estimate")
def estimate_job(
    project_id: UUID,
    payload: GenerationCreate,
    db: Database,
    actor: Actor,
) -> dict:
    require_project(db, project_id, actor, write=True)
    from arclight.application.generation_jobs import validate_target

    validate_target(db, project_id, payload)
    try:
        quote = quote_job(db, project_id, payload, actor=actor)
    except DomainError as exc:
        if exc.code != "BUDGET_QUOTE_UNAVAILABLE":
            raise
        return {
            "estimate": None,
            "allowed": False,
            "reason": exc.code,
            "message": "无法准确估算，收费任务暂不能提交",
        }
    try:
        enforce_budget(db, project_id, payload, quote.maximum_cost)
    except DomainError as exc:
        if not exc.code.startswith("BUDGET_"):
            raise
        return {
            "estimate": quote.model_dump(mode="json"),
            "allowed": False,
            "reason": exc.code,
            "message": exc.message,
        }
    current = usage(db, project_id)
    projected = current["projected_cost"] + quote.maximum_cost
    warning = projected > 0 and (
        projected * 100 >= current["project_limit"] * current["warning_percent"]
    )
    return {
        "estimate": quote.model_dump(mode="json"),
        "allowed": True,
        "reason": None,
        "warning": warning,
        "projected_cost": str(projected),
    }


@router.get("/jobs/{job_id}")
def captured_quote(project_id: UUID, job_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    job = db.get(GenerationJob, job_id)
    if job is None or job.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    quote = db.scalar(select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id))
    if quote is None:
        return {"estimate": None, "message": "旧任务未保存报价"}
    return {
        "estimate": {
            "currency": quote.currency,
            "maximum_cost": str(quote.maximum_cost),
            "pricing_version": quote.pricing_version,
        },
        "batch_id": quote.batch_id,
    }


def ledger_read(
    row: GenerationBudgetReservation, settlement: GenerationBudgetSettlement | None
) -> dict:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "attempt_no": row.attempt_no,
        "sequence": row.sequence,
        "reserved": format(row.amount, ".6f"),
        "currency": row.currency,
        "status": "SETTLED" if settlement is not None else "HELD",
        "cost": format(settlement.cost, ".6f") if settlement is not None else None,
        "released": format(row.amount - settlement.cost, ".6f") if settlement is not None else None,
        "source": settlement.source if settlement is not None else row.source,
        "evidence_reference": settlement.evidence_reference if settlement is not None else None,
    }


@router.get("/ledger")
def read_ledger(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[dict]:
    require_project(db, project_id, actor)
    rows = db.execute(
        select(GenerationBudgetReservation, GenerationBudgetSettlement)
        .join(GenerationJob, GenerationJob.id == GenerationBudgetReservation.job_id)
        .outerjoin(
            GenerationBudgetSettlement,
            GenerationBudgetSettlement.reservation_id == GenerationBudgetReservation.id,
        )
        .where(GenerationJob.project_id == project_id)
        .order_by(GenerationBudgetReservation.created_at.desc(), GenerationBudgetReservation.id)
        .limit(limit)
        .offset(offset)
    )
    return [ledger_read(row, settlement) for row, settlement in rows]


@router.post("/reservations/{reservation_id}/settlement")
def confirm_billing(
    project_id: UUID,
    reservation_id: UUID,
    payload: BudgetSettlementInput,
    db: Database,
    actor: Actor,
) -> dict:
    require_project(db, project_id, actor, write=True, owner=True)
    row = db.get(GenerationBudgetReservation, reservation_id)
    job = db.get(GenerationJob, row.job_id) if row is not None else None
    if row is None or job is None or job.project_id != project_id:
        raise DomainError("NOT_FOUND", "预算预留不存在", 404)
    attempt = db.scalar(
        select(GenerationAttempt).where(
            GenerationAttempt.job_id == job.id,
            GenerationAttempt.attempt_no == row.attempt_no,
        )
    )
    if attempt is None or attempt.finished_at is None:
        raise DomainError(
            "BUDGET_EXECUTION_UNRESOLVED", "请先对账确认调用结果，再核验最终账单", 409
        )
    from arclight.application.generation_accounting import settle

    settlement = settle(
        db, job, row, payload.cost, "MANUAL", actor=actor, evidence=payload.evidence_reference
    )
    return ledger_read(row, settlement)
