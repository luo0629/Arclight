"""Database-serialized admission and persistent, bounded retry decisions."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application import generation_leases as leases
from arclight.application.audit import audit
from arclight.application.generation_events import enqueue_dispatch
from arclight.application.generation_state import transition
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_scheduling import (
    DEFAULT_EXECUTION_POLICY,
    ERROR_CATEGORIES,
    ExecutionPolicy,
)
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.generation_scheduling import (
    GenerationCall,
    GenerationDeadLetter,
    GenerationSchedule,
    GenerationSchedulerGate,
)


def schedule_for(
    db: Session, job: GenerationJob, policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY
) -> GenerationSchedule:
    row = db.scalar(select(GenerationSchedule).where(GenerationSchedule.job_id == job.id))
    if row is None:
        snapshot = policy.model_dump(mode="json")
        snapshot["project_limits"] = {
            str(job.project_id): policy.project_limits.get(
                str(job.project_id), policy.project_concurrency
            )
        }
        row = GenerationSchedule(
            job_id=job.id,
            policy_json=snapshot,
            deadline_at=leases.utc(job.created_at) + timedelta(seconds=policy.max_elapsed_seconds),
            next_attempt_at=leases.clock(db),
        )
        db.add(row)
        db.flush()
    return row


def policy_for(db: Session, job: GenerationJob) -> ExecutionPolicy:
    return ExecutionPolicy.model_validate(schedule_for(db, job).policy_json)


def diagnose(
    db: Session, job: GenerationJob, reason: str, error: str | None = None
) -> GenerationDeadLetter:
    row = db.scalar(select(GenerationDeadLetter).where(GenerationDeadLetter.job_id == job.id))
    if row is None:
        attempt = db.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == job.id,
                GenerationAttempt.attempt_no == job.attempt_count,
            )
        )
        schedule = schedule_for(db, job)
        row = GenerationDeadLetter(
            job_id=job.id,
            attempt_id=attempt.id if attempt else None,
            reason=reason,
            error_code=error,
            category=ERROR_CATEGORIES.get(error or "", "SCHEDULING"),
            diagnostic_json={
                "attempt_count": job.attempt_count,
                "status": job.status,
                "deadline_at": leases.utc(schedule.deadline_at).isoformat(),
                "policy": schedule.policy_json,
            },
        )
        db.add(row)
        db.flush()
        audit(
            db,
            job.project_id,
            "media-scheduler",
            "generation.dead_lettered",
            job.id,
            {"dead_letter_id": str(row.id), "reason": reason, "error_code": error},
        )
    return row


def within_limits(
    db: Session, job: GenerationJob, *, submitting: bool = False, error: str | None = None
) -> bool:
    schedule = schedule_for(db, job)
    policy = ExecutionPolicy.model_validate(schedule.policy_json)
    reason = None
    if leases.clock(db) >= leases.utc(schedule.deadline_at):
        reason = "TIME_LIMIT"
    elif job.attempt_count >= policy.max_attempts:
        reason = "ATTEMPT_LIMIT"
    if reason is None:
        return True
    schedule.blocked_code = reason
    diagnose(db, job, reason, error)
    if submitting and job.status in {Status.CREATED, Status.RETRY_WAIT}:
        # No current submission exists in either queue state: fail only the job.
        transition(db, job.id, Status.FAILED, job.revision)
    return False


def retry_after_failure(
    db: Session,
    job: GenerationJob,
    error: str,
    *,
    automatic: bool = True,
    not_accepted: bool = False,
    actor: str = "media-scheduler",
) -> bool:
    if job.cancel_requested_at is not None:
        return False
    if not within_limits(db, job, error=error):
        return False
    policy = policy_for(db, job)
    if error not in policy.retry_errors:
        diagnose(db, job, "NOT_RETRYABLE", error)
        return False
    if automatic and not not_accepted and error not in policy.automatic_retry_errors:
        return False
    schedule = schedule_for(db, job)
    schedule.next_attempt_at = leases.clock(db) + timedelta(
        seconds=policy.backoff(job.attempt_count)
    )
    schedule.blocked_code = "BACKOFF"
    transition(db, job.id, Status.RETRY_WAIT, job.revision, actor=actor)
    enqueue_dispatch(db, job)
    return True


def admit(db: Session, job: GenerationJob, live: ExecutionPolicy, kind: str = "SUBMIT") -> bool:
    """Call with the project/job locked; the singleton serializes count + reservation."""
    gate = db.get(GenerationSchedulerGate, "global", with_for_update=True)
    assert gate is not None
    stamp = leases.clock(db)
    schedule = schedule_for(db, job, live)
    if kind != "SUBMIT":
        # Read-only/control calls abandoned by a dead worker cannot occupy slots forever.
        # The process-local semaphore still bounds physically stuck adapter threads.
        stale = db.scalars(
            select(GenerationCall).where(
                GenerationCall.kind != "SUBMIT",
                GenerationCall.released_at.is_(None),
                GenerationCall.started_at <= stamp - timedelta(seconds=3600),
            )
        )
        for call in stale:
            complete_call(db, call, "ABANDONED")
    if kind == "SUBMIT":
        if not within_limits(db, job, submitting=True):
            return False
        if leases.utc(schedule.next_attempt_at) > stamp:
            return False
    block = None
    if kind == "SUBMIT" and job.provider in live.paused_providers:
        block = "PROVIDER_PAUSED"
    active = (
        select(func.count())
        .select_from(GenerationCall)
        .where(
            GenerationCall.released_at.is_(None),
            GenerationCall.kind == kind,
        )
    )
    provider_limit = (
        live.provider_limits.get(job.provider, live.provider_concurrency) if kind == "SUBMIT" else 4
    )
    project_limit = live.project_limits.get(str(job.project_id), live.project_concurrency)
    if (db.scalar(active.where(GenerationCall.provider == job.provider)) or 0) >= provider_limit:
        block = "PROVIDER_CONCURRENCY"
    elif (
        kind == "SUBMIT"
        and (db.scalar(active.where(GenerationCall.project_id == job.project_id)) or 0)
        >= project_limit
    ):
        block = "PROJECT_CONCURRENCY"
    recent = (
        select(func.count())
        .select_from(GenerationCall)
        .where(GenerationCall.started_at > stamp - timedelta(seconds=60))
    )
    if (db.scalar(recent) or 0) >= live.global_per_minute:
        block = "GLOBAL_RATE"
    elif (
        db.scalar(recent.where(GenerationCall.actor == job.created_by)) or 0
    ) >= live.user_per_minute:
        block = "USER_RATE"
    if block:
        if kind == "SUBMIT":
            schedule.blocked_code = block
            schedule.next_attempt_at = stamp + timedelta(seconds=1)
        return False
    if kind == "SUBMIT":
        schedule.blocked_code = None
    return True


def reserve(db: Session, job: GenerationJob, attempt_id: UUID, kind: str) -> GenerationCall:
    row = GenerationCall(
        job_id=job.id,
        attempt_id=attempt_id,
        project_id=job.project_id,
        provider=job.provider,
        actor=job.created_by,
        kind=kind,
        started_at=leases.clock(db),
    )
    db.add(row)
    db.flush()
    return row


def complete_call(db: Session, call: GenerationCall, outcome: str) -> None:
    if call.completed_at is None:
        call.completed_at = leases.clock(db)
        call.outcome = outcome
    if call.kind != "SUBMIT" and call.released_at is None:
        call.released_at = leases.clock(db)
    db.flush()


def release_generation(db: Session, job: GenerationJob) -> None:
    """Only authoritative terminal evidence may release generation occupancy."""
    rows = db.scalars(
        select(GenerationCall).where(
            GenerationCall.job_id == job.id,
            GenerationCall.kind == "SUBMIT",
            GenerationCall.released_at.is_(None),
        )
    )
    for row in rows:
        if row.completed_at is None:
            complete_call(db, row, job.status)
        row.released_at = leases.clock(db)
    db.flush()
