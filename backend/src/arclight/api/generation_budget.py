"""Media estimates and immutable billing evidence."""

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.application.commands import require_project
from arclight.application.generation_budget import quote_job
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import BudgetSettlementInput
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.generation import GenerationAttempt
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.persistence.models import GenerationJob

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/generation-costs", tags=["generation-costs"]
)


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
    quote = quote_job(db, payload, actor=actor)
    return {"estimate": quote.model_dump(mode="json") if quote is not None else None}


@router.get("/jobs/{job_id}")
def captured_quote(project_id: UUID, job_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    job = db.get(GenerationJob, job_id)
    if job is None or job.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    quote = db.scalar(select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id))
    if quote is None:
        return {"estimate": None}
    return {
        "estimate": {
            "currency": quote.currency,
            "maximum_cost": str(quote.maximum_cost),
            "pricing_version": quote.pricing_version,
        },
    }


def ledger_read(
    row: GenerationBudgetReservation, settlement: GenerationBudgetSettlement | None
) -> dict:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "attempt_no": row.attempt_no,
        "sequence": row.sequence,
        "reserved": format(row.amount, ".6f") if row.amount is not None else None,
        "currency": row.currency,
        "status": "SETTLED" if settlement is not None else "HELD",
        "cost": format(settlement.cost, ".6f") if settlement is not None else None,
        "released": format(row.amount - settlement.cost, ".6f")
        if settlement is not None and row.amount is not None
        else None,
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
