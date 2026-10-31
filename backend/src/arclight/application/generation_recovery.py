"""Restart-safe recovery polls existing attempts and never submits a generation."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application import generation_leases as leases
from arclight.application.generation_reconciliation import mark_result_unknown, reconcile
from arclight.application.generation_state import find_job
from arclight.core.errors import DomainError
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.persistence.base import utcnow
from arclight.persistence.generation import (
    GenerationAttempt,
    GenerationDispatch,
    GenerationJob,
    GenerationLease,
)
from arclight.providers.media_cancel import MediaCancelProvider
from arclight.providers.media_query import MediaQueryProvider
from arclight.providers.media_submission import SubmissionReceipt

RECOVERABLE = {
    "SUBMITTING",
    "SUBMITTED",
    "RUNNING",
    "CANCEL_REQUESTED",
    "RESULT_UNKNOWN",
    "RECONCILING",
}


def recover_job(
    factory: sessionmaker[Session],
    job_id: UUID,
    provider: MediaQueryProvider,
    *,
    policy: leases.LeasePolicy = leases.DEFAULT_POLICY,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    cancel_provider: MediaCancelProvider | None = None,
) -> str:
    with factory.begin() as db:
        job = find_job(db, job_id, lock=True)
        if job.status not in RECOVERABLE:
            return job.status
        old = leases.lease_for(db, job.id)
        stamp = leases.clock(db)
        if old is not None and leases.utc(old.expires_at) > stamp:
            return job.status
        attempt = db.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == job.id,
                GenerationAttempt.attempt_no == job.attempt_count,
            )
        )
        if attempt is None or attempt.finished_at is not None:
            return job.status
        lease = leases.claim(db, job, attempt.id, policy)
        if lease is None:
            return job.status
        token = lease.token
        # A CLAIMED dispatch may have reached the provider even if no receipt survived.
        event = db.scalar(
            select(GenerationDispatch)
            .where(
                GenerationDispatch.attempt_id == attempt.id,
                GenerationDispatch.status == "CLAIMED",
            )
            .with_for_update()
        )
        if event is not None:
            event.status = "FINISHED"
            event.receipt_json = SubmissionReceipt(
                status="RESULT_UNKNOWN",
                error_code="RESULT_UNKNOWN",
            ).model_dump(mode="json")
            event.finished_at = utcnow()
        if job.status != "RESULT_UNKNOWN":
            mark_result_unknown(db, job.id, attempt.id)
        revision, project_id = job.revision, job.project_id

    try:
        with leases.keep_alive(factory, job_id, token, policy):
            row = reconcile(
                factory,
                project_id,
                job_id,
                revision,
                "media-recovery",
                provider,
                lease_token=token,
                lease_policy=policy,
                execution_policy=execution_policy,
                cancel_provider=cancel_provider,
            )
        return row.status
    except DomainError as exc:
        if exc.code not in {
            "VERSION_CONFLICT",
            "GENERATION_LEASE_LOST",
            "GENERATION_NOT_RECONCILABLE",
        }:
            raise
        with factory() as db:
            return find_job(db, job_id).status


def recovery_candidates(factory: sessionmaker[Session], policy: leases.LeasePolicy) -> list[UUID]:
    with factory() as db:
        stamp = leases.clock(db)
        return list(
            db.scalars(
                select(GenerationJob.id)
                .outerjoin(GenerationLease, GenerationLease.job_id == GenerationJob.id)
                .where(
                    GenerationJob.status.in_(RECOVERABLE),
                    GenerationJob.attempt_count > 0,
                    or_(
                        GenerationLease.expires_at <= stamp,
                        (GenerationLease.id.is_(None))
                        & (
                            GenerationJob.updated_at
                            <= stamp - timedelta(seconds=policy.duration_seconds)
                        ),
                    ),
                )
                .order_by(GenerationJob.updated_at, GenerationJob.id)
                .limit(100)
            )
        )
