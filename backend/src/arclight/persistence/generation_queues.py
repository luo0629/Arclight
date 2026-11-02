"""Queue identity, restart-safe fairness and fenced, preallocated dispatch permits."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class GenerationLane(Entity, Base):
    __tablename__ = "generation_lanes"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    lane: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[int] = mapped_column(default=1)
    actor: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("lane IN ('INTERACTIVE','BATCH')", name="generation_lane"),
        CheckConstraint("policy_version = 1", name="generation_lane_version"),
    )


class GenerationQueueState(Base):
    __tablename__ = "generation_queue_state"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    turn: Mapped[int] = mapped_column(default=0)
    interactive_cursor: Mapped[str] = mapped_column(String(200), default="")
    batch_cursor: Mapped[str] = mapped_column(String(200), default="")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_publish_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("id = 'global'", name="generation_queue_singleton"),)


class GenerationPermit(Entity, Base):
    __tablename__ = "generation_permits"
    dispatch_id: Mapped[UUID] = mapped_column(ForeignKey("generation_dispatches.id"), unique=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    provider: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    lane: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="WAITING")
    epoch: Mapped[int] = mapped_column(default=0)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_count: Mapped[int] = mapped_column(default=0)
    reason: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("lane IN ('INTERACTIVE','BATCH')", name="generation_permit_lane"),
        CheckConstraint(
            "state IN ('WAITING','ISSUED','CONSUMED','REVOKED')", name="generation_permit_state"
        ),
        CheckConstraint("epoch >= 0 AND publish_count >= 0", name="generation_permit_counters"),
        CheckConstraint(
            "state != 'ISSUED' OR (issued_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND publish_after IS NOT NULL AND epoch > 0)",
            name="generation_permit_issued",
        ),
        Index("ix_generation_permits_capacity", "state", "expires_at", "provider"),
    )


class GenerationPermitEvent(Entity, Base):
    __tablename__ = "generation_permit_events"
    permit_id: Mapped[UUID] = mapped_column(ForeignKey("generation_permits.id"), index=True)
    sequence: Mapped[int]
    epoch: Mapped[int]
    action: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("permit_id", "sequence"),)
