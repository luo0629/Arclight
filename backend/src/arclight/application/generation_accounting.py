"""Project-serialized budget reservations and append-only final billing facts."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.core.errors import DomainError
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.generation_budget import GenerationBudgetQuote


def reservation_for(
    db: Session, job_id: UUID, attempt_no: int
) -> GenerationBudgetReservation | None:
    return db.scalar(
        select(GenerationBudgetReservation)
        .where(
            GenerationBudgetReservation.job_id == job_id,
            GenerationBudgetReservation.attempt_no == attempt_no,
        )
        .order_by(GenerationBudgetReservation.sequence.desc())
        .limit(1)
    )


def reserve_next(db: Session, job: GenerationJob, amount: Decimal) -> GenerationBudgetReservation:
    """Caller holds the project lock and has checked scoped budget admission."""
    row = reservation_for(db, job.id, job.attempt_count + 1)
    if row is not None:
        settled = db.scalar(
            select(GenerationBudgetSettlement).where(
                GenerationBudgetSettlement.reservation_id == row.id,
            )
        )
        if settled is not None and settled.source == "PRE_SUBMISSION" and settled.cost == 0:
            sequence = row.sequence + 1
        elif row.amount != amount or settled is not None:
            raise DomainError("BUDGET_RESERVATION_CONFLICT", "预算预留已处理或金额不匹配", 409)
        else:
            return row
    else:
        sequence = 1
    row = GenerationBudgetReservation(
        job_id=job.id,
        attempt_no=job.attempt_count + 1,
        sequence=sequence,
        amount=amount,
        currency="USD",
        source="ADMISSION" if job.attempt_count == 0 else "RETRY",
    )
    db.add(row)
    db.flush()
    audit(
        db,
        job.project_id,
        "budget-runtime",
        "budget.reserved",
        job.id,
        {
            "reservation_id": str(row.id),
            "attempt_no": row.attempt_no,
            "sequence": row.sequence,
            "amount": format(amount, ".6f"),
            "currency": "USD",
        },
    )
    return row


def settle(
    db: Session,
    job: GenerationJob,
    reservation: GenerationBudgetReservation,
    cost: Decimal,
    source: str,
    *,
    actor: str = "budget-runtime",
    evidence: str | None = None,
) -> GenerationBudgetSettlement:
    previous = db.scalar(
        select(GenerationBudgetSettlement).where(
            GenerationBudgetSettlement.reservation_id == reservation.id,
        )
    )
    if previous is not None:
        if (
            previous.cost != cost
            or previous.source != source
            or previous.evidence_reference != evidence
        ):
            raise DomainError(
                "BUDGET_SETTLEMENT_CONFLICT", "已有不同费用证据，不能覆盖历史结算", 409
            )
        return previous
    row = GenerationBudgetSettlement(
        reservation_id=reservation.id,
        cost=cost,
        source=source,
        evidence_reference=evidence,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        job.project_id,
        actor,
        "budget.settled",
        job.id,
        {
            "reservation_id": str(reservation.id),
            "settlement_id": str(row.id),
            "attempt_no": reservation.attempt_no,
            "source": source,
            "cost": format(cost, ".6f"),
            "released": format(reservation.amount - cost, ".6f"),
            "over_quote": cost > reservation.amount,
            "evidence_reference": evidence,
        },
    )
    return row


def settle_attempt(
    db: Session,
    job: GenerationJob,
    attempt: GenerationAttempt,
    *,
    not_accepted: bool = False,
) -> None:
    reservation = reservation_for(db, job.id, attempt.attempt_no)
    if reservation is None:
        # Pre-quote historical calls retain their original usage, without fabricated prices.
        return
    if attempt.cost is not None:
        settle(db, job, reservation, attempt.cost, "PROVIDER")
    elif not_accepted:
        settle(db, job, reservation, Decimal("0"), "NOT_ACCEPTED")
    elif reservation.amount == 0:
        settle(db, job, reservation, Decimal("0"), "FREE")
    else:
        audit(
            db,
            job.project_id,
            "budget-runtime",
            "budget.billing_pending",
            job.id,
            {
                "reservation_id": str(reservation.id),
                "attempt_no": attempt.attempt_no,
            },
        )


def release_unsubmitted(db: Session, job: GenerationJob) -> None:
    reservation = reservation_for(db, job.id, job.attempt_count + 1)
    if reservation is not None:
        settle(db, job, reservation, Decimal("0"), "PRE_SUBMISSION")


def costs(
    db: Session,
    project_id: UUID,
    *,
    episode_id: UUID | None = None,
    batch_id: UUID | None = None,
    job_id: UUID | None = None,
    exclude_pending_job: UUID | None = None,
) -> tuple[Decimal, Decimal]:
    statement = (
        select(GenerationBudgetReservation, GenerationBudgetSettlement, GenerationJob)
        .join(GenerationJob, GenerationJob.id == GenerationBudgetReservation.job_id)
        .outerjoin(
            GenerationBudgetSettlement,
            GenerationBudgetSettlement.reservation_id == GenerationBudgetReservation.id,
        )
        .outerjoin(GenerationBudgetQuote, GenerationBudgetQuote.job_id == GenerationJob.id)
        .where(GenerationJob.project_id == project_id)
    )
    if episode_id is not None:
        statement = statement.where(GenerationJob.episode_id == episode_id)
    if batch_id is not None:
        statement = statement.where(GenerationBudgetQuote.batch_id == batch_id)
    if job_id is not None:
        statement = statement.where(GenerationJob.id == job_id)
    actual = pending = Decimal("0")
    for reservation, settlement, job in db.execute(statement):
        if settlement is not None:
            actual += settlement.cost
        elif not (
            job.id == exclude_pending_job and reservation.attempt_no == job.attempt_count + 1
        ):
            pending += reservation.amount
    # Costs predating quotes still count, but their historical records remain untouched.
    legacy = (
        select(GenerationAttempt.cost)
        .join(GenerationJob)
        .outerjoin(
            GenerationBudgetReservation,
            (GenerationBudgetReservation.job_id == GenerationJob.id)
            & (GenerationBudgetReservation.attempt_no == GenerationAttempt.attempt_no),
        )
        .where(GenerationJob.project_id == project_id, GenerationBudgetReservation.id.is_(None))
    )
    if episode_id is not None:
        legacy = legacy.where(GenerationJob.episode_id == episode_id)
    if job_id is not None:
        legacy = legacy.where(GenerationJob.id == job_id)
    if batch_id is None:
        actual += sum((value for value in db.scalars(legacy) if value is not None), Decimal("0"))
    if batch_id is None and job_id is None:
        from arclight.application.gateway_accounting import costs as gateway_costs

        text_actual, text_pending = gateway_costs(db, project_id, episode_id=episode_id)
        actual += text_actual
        pending += text_pending
    return actual, pending
