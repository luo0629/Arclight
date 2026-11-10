"""Short database-clock claims, cancellation observation and fenced renderer writes."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.generation_leases import LeasePolicy, clock, utc
from arclight.application.render_job_store import lock_job
from arclight.core.errors import DomainError
from arclight.persistence.models import RenderAttempt, RenderExecution, RenderJob

POLICY = LeasePolicy()
MAX_SECONDS = 1800


def owned(db: Session, run: RenderExecution, job: RenderJob, token: UUID) -> None:
    if (
        run.status != "RUNNING"
        or job.lease_token != token
        or job.lease_expires_at is None
        or utc(job.lease_expires_at) <= clock(db)
    ):
        raise DomainError("RENDER_LEASE_LOST", "渲染执行权已过期", 409)


def finish(
    db: Session, run: RenderExecution, job: RenderJob, status: str, code: str | None
) -> None:
    stamp = clock(db)
    attempt = db.get(RenderAttempt, job.lease_token) if job.lease_token else None
    if attempt is not None and attempt.status == "RUNNING":
        attempt.status, attempt.phase = status, status
        attempt.error_code, attempt.finished_at = code, stamp
    run.status, run.error_code, run.finished_at = status, code, stamp
    job.phase, job.revision = status, job.revision + 1
    job.lease_token, job.lease_expires_at = None, None
    db.flush()


def claim(
    factory: sessionmaker[Session], run_id: UUID, policy: LeasePolicy = POLICY
) -> UUID | None:
    with factory.begin() as db:
        run, job = lock_job(db, run_id)
        stamp = clock(db)
        if run.status not in {"QUEUED", "RUNNING"}:
            return None
        if run.status == "RUNNING":
            if job.lease_expires_at and utc(job.lease_expires_at) > stamp:
                return None
            old = db.get(RenderAttempt, job.lease_token)
            if old is not None:
                old.status, old.phase = "INTERRUPTED", "INTERRUPTED"
                old.error_code, old.finished_at = "RENDER_WORKER_INTERRUPTED", stamp
            # Flush the historical attempt before changing the current claim.
            db.flush()
        if job.cancel_requested_at:
            finish(db, run, job, "CANCELLED", None)
            return None
        if job.attempt_count >= 3 or (
            job.started_at and (stamp - utc(job.started_at)).total_seconds() >= MAX_SECONDS
        ):
            finish(db, run, job, "FAILED", "RENDER_RECOVERY_EXHAUSTED")
            return None
        token = uuid4()
        run.status, run.error_code = "RUNNING", None
        job.attempt_count += 1
        job.started_at = job.started_at or stamp
        job.lease_token = token
        job.lease_expires_at = stamp + timedelta(seconds=policy.duration_seconds)
        job.phase, job.progress, job.revision = "ADMISSION", 0, job.revision + 1
        db.flush()
        db.add(
            RenderAttempt(
                id=token,
                run_id=run_id,
                attempt_no=job.attempt_count,
                output_key=f"rendered/{job.project_id}/{token}",
            )
        )
        return token


def observe(
    factory: sessionmaker[Session],
    run_id: UUID,
    token: UUID,
    progress: int | None = None,
    *,
    phase: str | None = None,
) -> None:
    with factory.begin() as db:
        run, job = lock_job(db, run_id)
        owned(db, run, job, token)
        if job.cancel_requested_at:
            raise DomainError("RENDER_CANCELLED", "渲染已请求取消", 409)
        if job.started_at and (clock(db) - utc(job.started_at)).total_seconds() >= MAX_SECONDS:
            raise DomainError("RENDER_TIME_LIMIT", "渲染超过总执行时限", 409)
        attempt = db.get(RenderAttempt, token)
        assert attempt is not None
        value = max(job.progress, min(99, progress)) if progress is not None else job.progress
        next_phase = phase or job.phase
        if (value, next_phase) != (job.progress, job.phase):
            job.progress, job.phase, job.revision = value, next_phase, job.revision + 1
            attempt.progress, attempt.phase = value, next_phase


@contextmanager
def keep_alive(
    factory: sessionmaker[Session], run_id: UUID, token: UUID, policy: LeasePolicy = POLICY
) -> Iterator[None]:
    stop = Event()

    def renew() -> None:
        while not stop.wait(policy.heartbeat_seconds):
            try:
                with factory.begin() as db:
                    run, job = lock_job(db, run_id)
                    owned(db, run, job, token)
                    job.lease_expires_at = clock(db) + timedelta(seconds=policy.duration_seconds)
            except (SQLAlchemyError, DomainError):
                return

    thread = Thread(target=renew, name=f"render-heartbeat-{run_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
