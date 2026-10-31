"""Media execution records, independent of text workflows and selected assets."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity

STATES_SQL = (
    "'CREATED', 'SUBMITTING', 'SUBMITTED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', "
    "'FAILED', 'CANCEL_REQUESTED', 'CANCELLED', 'RESULT_UNKNOWN', 'RECONCILING', 'MANUAL_REVIEW'"
)


class GenerationJob(Entity, Base):
    __tablename__ = "generation_jobs"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    input_manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("dependency_manifests.id"), index=True
    )
    task_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    revision: Mapped[int] = mapped_column(default=0)
    attempt_count: Mapped[int] = mapped_column(default=0)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_asset_revision: Mapped[int | None]
    expected_lock_version: Mapped[int | None]
    expected_current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_versions.id")
    )
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_generation_job_idempotency"),
        CheckConstraint(f"status IN ({STATES_SQL})", name="generation_job_status"),
        CheckConstraint("revision >= 0 AND attempt_count >= 0", name="generation_job_counters"),
        CheckConstraint(
            "target_type IN ('CHARACTER', 'SCENE', 'PROP', 'STYLE', 'SHOT_IMAGE', "
            "'AUDIO', 'VIDEO', 'RENDER')",
            name="generation_target_type",
        ),
        CheckConstraint(
            "task_type IN ('IMAGE', 'AUDIO', 'VIDEO', 'RENDER')", name="generation_task_type"
        ),
        CheckConstraint(
            "(expected_asset_revision IS NULL OR expected_asset_revision >= 0) AND "
            "(expected_lock_version IS NULL OR expected_lock_version >= 0)",
            name="generation_expected_revisions",
        ),
        Index("ix_generation_jobs_status_created", "status", "created_at"),
        Index(
            "uq_generation_active_asset",
            "target_asset_id",
            unique=True,
            sqlite_where=text(
                "target_asset_id IS NOT NULL AND status IN "
                "('CANCEL_REQUESTED','CREATED','MANUAL_REVIEW','RECONCILING','RESULT_UNKNOWN',"
                "'RETRY_WAIT','RUNNING','SUBMITTED','SUBMITTING')"
            ),
            postgresql_where=text(
                "target_asset_id IS NOT NULL AND status IN "
                "('CANCEL_REQUESTED','CREATED','MANUAL_REVIEW','RECONCILING','RESULT_UNKNOWN',"
                "'RETRY_WAIT','RUNNING','SUBMITTED','SUBMITTING')"
            ),
        ),
    )


class GenerationAttempt(Entity, Base):
    __tablename__ = "generation_attempts"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    attempt_no: Mapped[int]
    input_manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("dependency_manifests.id"), index=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(160))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTING")
    input_snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    result_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None]
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_generation_attempt_no"),
        UniqueConstraint("provider", "provider_request_id", name="uq_generation_provider_request"),
        CheckConstraint("attempt_no > 0", name="generation_attempt_positive"),
        CheckConstraint(f"status IN ({STATES_SQL})", name="generation_attempt_status"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="generation_latency"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="generation_cost"),
        Index("ix_generation_attempts_status_created", "status", "created_at"),
    )


class GenerationReconciliation(Entity, Base):
    __tablename__ = "generation_reconciliations"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"), index=True)
    claimed_revision: Mapped[int]
    source: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(100))
    evidence_reference: Mapped[str | None] = mapped_column(String(200))
    receipt_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), default="PENDING")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "job_id", "claimed_revision", name="uq_generation_reconciliation_revision"
        ),
        CheckConstraint("claimed_revision >= 0", name="generation_reconciliation_revision"),
        CheckConstraint(
            "source IN ('PROVIDER', 'MANUAL')", name="generation_reconciliation_source"
        ),
        CheckConstraint(
            "(disposition = 'PENDING' AND finished_at IS NULL AND receipt_json IS NULL) "
            "OR (disposition IN ('APPLIED', 'SUPERSEDED') "
            "AND finished_at IS NOT NULL AND receipt_json IS NOT NULL)",
            name="generation_reconciliation_completion",
        ),
    )


class GenerationDispatch(Entity, Base):
    __tablename__ = "generation_dispatches"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    expected_revision: Mapped[int]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="READY")
    attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id"), index=True
    )
    provider_supports_idempotency: Mapped[bool | None]
    provider_idempotency_key: Mapped[str | None] = mapped_column(String(160))
    receipt_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("job_id", "expected_revision", name="uq_generation_dispatch_revision"),
        CheckConstraint("expected_revision >= 0", name="generation_dispatch_revision"),
        CheckConstraint(
            "status IN ('READY','CLAIMED','FINISHED','SKIPPED')", name="generation_dispatch_status"
        ),
        CheckConstraint(
            "(status IN ('READY','CLAIMED') AND finished_at IS NULL AND receipt_json IS NULL) "
            "OR (status IN ('FINISHED','SKIPPED') "
            "AND finished_at IS NOT NULL AND receipt_json IS NOT NULL)",
            name="generation_dispatch_completion",
        ),
        CheckConstraint(
            "(status IN ('READY','SKIPPED') AND attempt_id IS NULL "
            "AND provider_supports_idempotency IS NULL AND provider_idempotency_key IS NULL) "
            "OR (status IN ('CLAIMED','FINISHED') AND attempt_id IS NOT NULL "
            "AND provider_supports_idempotency IS NOT NULL "
            "AND ((provider_supports_idempotency AND provider_idempotency_key IS NOT NULL) "
            "OR (NOT provider_supports_idempotency AND provider_idempotency_key IS NULL)))",
            name="generation_dispatch_claim",
        ),
    )


class GenerationLease(Entity, Base):
    __tablename__ = "generation_leases"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("generation_jobs.id"), unique=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("generation_attempts.id"))
    token: Mapped[UUID]
    epoch: Mapped[int]
    owner: Mapped[str] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("epoch > 0", name="generation_lease_epoch"),
        CheckConstraint("expires_at > heartbeat_at", name="generation_lease_expiry"),
    )
