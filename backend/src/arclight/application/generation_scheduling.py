"""Database-serialized admission and persistent, bounded retry decisions."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application import generation_leases as leases
from arclight.application.audit import audit
from arclight.application.generation_events import enqueue_dispatch
from arclight.application.generation_state import transition
from arclight.core.errors import DomainError
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
)


def schedule_for(
    db: Session,
    job: GenerationJob,
    policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    *,
    parent_deadline: datetime | None = None,
) -> GenerationSchedule:
    row = db.scalar(select(GenerationSchedule).where(GenerationSchedule.job_id == job.id))
    if row is None:
        snapshot = policy.model_dump(mode="json")
        snapshot["project_limits"] = {
            str(job.project_id): policy.project_limits.get(
                str(job.project_id), policy.project_concurrency
            )
        }
        deadline = leases.utc(job.created_at) + timedelta(seconds=policy.max_elapsed_seconds)
        if parent_deadline is not None:
            deadline = min(deadline, leases.utc(parent_deadline))
        row = GenerationSchedule(
            job_id=job.id,
            policy_json=snapshot,
            deadline_at=deadline,
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
    from arclight.application.generation_budget import check_execution
    from arclight.domain.generation_requests import GenerationSpec
    from arclight.persistence.models import DependencyManifest

    manifest = db.get(DependencyManifest, job.input_manifest_id)
    assert manifest is not None
    spec = GenerationSpec.model_validate(manifest.snapshot_json["context"]["generation"])
    try:
        check_execution(db, job, spec)
    except DomainError:
        if not automatic:
            raise
        diagnose(db, job, "NOT_RETRYABLE", "BUDGET_EXCEEDED")
        return False
    schedule = schedule_for(db, job)
    schedule.next_attempt_at = leases.clock(db) + timedelta(
        seconds=policy.backoff(job.attempt_count)
    )
    schedule.blocked_code = "BACKOFF"
    transition(db, job.id, Status.RETRY_WAIT, job.revision, actor=actor)
    enqueue_dispatch(db, job)
    return True


def admit(
    db: Session,
    job: GenerationJob,
    live: ExecutionPolicy,
    kind: str = "SUBMIT",
    *,
    permit_id: UUID | None = None,
    permit_epoch: int | None = None,
    event_id: UUID | None = None,
) -> bool:
    """Call with the project/job locked; the singleton serializes count + reservation."""
    from arclight.application.generation_queues import capacity_block, lock_gate, valid_permit
    from arclight.persistence.generation_queues import GenerationLane

    schedule = schedule_for(db, job, live)
    lock_gate(db)
    identity = db.scalar(select(GenerationLane).where(GenerationLane.job_id == job.id))
    if permit_id is not None:
        permit = (
            valid_permit(db, event_id, permit_id, permit_epoch)
            if event_id is not None and permit_epoch is not None
            else None
        )
        if (
            permit is None
            or permit.job_id != job.id
            or permit.project_id != job.project_id
            or permit.provider != job.provider
            or permit.actor != job.created_by
            or identity is None
            or permit.lane != identity.lane
        ):
            return False
    stamp = leases.clock(db)
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
    block = capacity_block(
        db,
        job,
        live,
        kind,
        lane=identity.lane if identity else "BATCH",
        exclude_permit=permit_id,
    )
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
