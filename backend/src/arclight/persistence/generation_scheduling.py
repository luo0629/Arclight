"""Durable admission, bounded execution, diagnostics and authorized replay facts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class GenerationSchedulerGate(Base):
    __tablename__ = "generation_scheduler_gate"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    __table_args__ = (CheckConstraint("id = 'global'", name="generation_scheduler_singleton"),)


class GenerationSchedule(Entity, Base):
    __tablename__ = "generation_schedules"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    policy_json: Mapped[dict] = mapped_column(JSON_TYPE)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    blocked_code: Mapped[str | None] = mapped_column(String(64))


class GenerationCall(Entity, Base):
    __tablename__ = "generation_calls"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    provider: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("kind IN ('SUBMIT','QUERY','CANCEL')", name="generation_call_kind"),
        CheckConstraint(
            "(completed_at IS NULL AND outcome IS NULL) OR "
            "(completed_at IS NOT NULL AND outcome IS NOT NULL)",
            name="generation_call_outcome",
        ),
        Index("ix_generation_calls_provider_active", "provider", "kind", "released_at"),
        Index("ix_generation_calls_actor_started", "actor", "started_at"),
    )


class GenerationDeadLetter(Entity, Base):
    __tablename__ = "generation_dead_letters"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("generation_attempts.id"))
    reason: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    diagnostic_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        CheckConstraint(
            "reason IN ('ATTEMPT_LIMIT','TIME_LIMIT','NOT_RETRYABLE','RESULT_UNKNOWN')",
            name="generation_dead_letter_reason",
        ),
    )


class GenerationReplay(Entity, Base):
    __tablename__ = "generation_replays"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    source_job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    dead_letter_id: Mapped[UUID] = mapped_column(ForeignKey("generation_dead_letters.id"))
    new_job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    creation_key: Mapped[str] = mapped_column(String(160))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(1000))
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key", name="uq_generation_replay_key"),
    )
