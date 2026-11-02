"""Append-only per-attempt reservations and final billing evidence."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class GenerationBudgetReservation(Entity, Base):
    __tablename__ = "generation_budget_reservations"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    attempt_no: Mapped[int]
    sequence: Mapped[int]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source: Mapped[str] = mapped_column(String(20))
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", "sequence", name="uq_budget_reservation_attempt"),
        CheckConstraint("attempt_no > 0", name="budget_reservation_attempt"),
        CheckConstraint("sequence > 0", name="budget_reservation_sequence"),
        CheckConstraint("amount >= 0", name="budget_reservation_amount"),
        CheckConstraint("currency = 'USD'", name="budget_reservation_currency"),
        CheckConstraint(
            "source IN ('ADMISSION', 'RETRY', 'LEGACY')", name="budget_reservation_source"
        ),
    )


class GenerationBudgetSettlement(Entity, Base):
    __tablename__ = "generation_budget_settlements"
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("generation_budget_reservations.id"), unique=True
    )
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    source: Mapped[str] = mapped_column(String(20))
    evidence_reference: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("cost >= 0", name="budget_settlement_cost"),
        CheckConstraint(
            "source IN ('PROVIDER', 'NOT_ACCEPTED', 'PRE_SUBMISSION', 'FREE', 'MANUAL', 'LEGACY')",
            name="budget_settlement_source",
        ),
    )
