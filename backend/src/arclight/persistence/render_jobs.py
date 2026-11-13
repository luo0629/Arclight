"""Manifest extension of the existing renderer ledger, with fenced attempt history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class RenderJob(Entity, Base):
    __tablename__ = "render_jobs"
    id: Mapped[UUID] = mapped_column(ForeignKey("render_executions.id"), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(ForeignKey("render_manifests.id"), index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=1)
    attempt_count: Mapped[int] = mapped_column(default=0)
    progress: Mapped[int] = mapped_column(default=0)
    phase: Mapped[str] = mapped_column(String(32), default="QUEUED")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key", name="uq_render_job_creation"),
        CheckConstraint("revision > 0 AND attempt_count BETWEEN 0 AND 3", name="render_job_count"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="render_job_progress"),
    )


class RenderAttempt(Entity, Base):
    __tablename__ = "render_attempts"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("render_jobs.id"), index=True)
    attempt_no: Mapped[int]
    output_key: Mapped[str] = mapped_column(String(512), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    progress: Mapped[int] = mapped_column(default=0)
    phase: Mapped[str] = mapped_column(String(32), default="ADMISSION")
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    cleanup_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    cleanup_error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_no", name="uq_render_attempt_number"),
        CheckConstraint("attempt_no BETWEEN 1 AND 3", name="render_attempt_number"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="render_attempt_progress"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')",
            name="render_attempt_status",
        ),
        CheckConstraint(
            "cleanup_status IN ('PENDING','CLEANED','FAILED','RETAINED')",
            name="render_attempt_cleanup",
        ),
    )


class RenderInspection(Entity, Base):
    __tablename__ = "render_inspections"
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("render_attempts.id"), unique=True)
    manifest_id: Mapped[UUID] = mapped_column(ForeignKey("render_manifests.id"), index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    output_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"))
    passed: Mapped[bool]
    result_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (CheckConstraint("byte_size > 0", name="render_inspection_size"),)
