"""Budget configuration and immutable admission/adjustment facts."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class GenerationBudgetPolicy(Base):
    __tablename__ = "generation_budget_policies"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=0)
    task_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    episode_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    batch_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    warning_percent: Mapped[int] = mapped_column(default=80)
    max_candidates_per_target: Mapped[int] = mapped_column(default=64)
    __table_args__ = (
        CheckConstraint("revision >= 0", name="budget_policy_revision"),
        CheckConstraint("warning_percent BETWEEN 1 AND 100", name="budget_warning_percent"),
        CheckConstraint("max_candidates_per_target BETWEEN 1 AND 10000", name="budget_candidates"),
        CheckConstraint(
            "(task_limit IS NULL OR task_limit >= 0) AND "
            "(episode_limit IS NULL OR episode_limit >= 0) AND "
            "(batch_limit IS NULL OR batch_limit >= 0)",
            name="budget_policy_limits",
        ),
    )


class GenerationBudgetBatch(Entity, Base):
    __tablename__ = "generation_budget_batches"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID | None] = mapped_column(ForeignKey("episodes.id"))
    creation_key: Mapped[UUID]
    budget_limit: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key", name="uq_budget_batch_key"),
        CheckConstraint("budget_limit >= 0", name="budget_batch_limit"),
    )


class GenerationBudgetQuote(Entity, Base):
    __tablename__ = "generation_budget_quotes"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("generation_budget_batches.id"))
    maximum_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(3))
    pricing_version: Mapped[str] = mapped_column(String(100))
    candidate_count: Mapped[int]
    __table_args__ = (
        CheckConstraint("maximum_cost >= 0", name="budget_quote_cost"),
        CheckConstraint("currency = 'USD'", name="budget_quote_currency"),
        CheckConstraint("candidate_count BETWEEN 1 AND 64", name="budget_quote_candidates"),
    )


class GenerationBudgetAdjustment(Entity, Base):
    __tablename__ = "generation_budget_adjustments"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    creation_key: Mapped[UUID]
    requested_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reason: Mapped[str] = mapped_column(String(1000))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key", name="uq_budget_adjustment_key"),
        CheckConstraint("requested_limit >= 0", name="budget_adjustment_limit"),
    )


class GenerationBudgetDecision(Entity, Base):
    __tablename__ = "generation_budget_decisions"
    adjustment_id: Mapped[UUID] = mapped_column(
        ForeignKey("generation_budget_adjustments.id"), unique=True
    )
    approved: Mapped[bool]
    reason: Mapped[str] = mapped_column(String(1000))
    decided_by: Mapped[str] = mapped_column(String(100))
    policy_revision: Mapped[int]
