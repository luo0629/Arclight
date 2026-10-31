"""Database-clock leases and fencing, with independent heartbeat transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.audit import audit
from arclight.persistence.generation import GenerationJob, GenerationLease


@dataclass(frozen=True)
class LeasePolicy:
    duration_seconds: float = 60
    heartbeat_seconds: float = 10
    poll_seconds: float = 30

    def __post_init__(self) -> None:
        if not 0 < self.heartbeat_seconds < self.duration_seconds / 2 or self.poll_seconds <= 0:
            raise ValueError(
                "Heartbeat must be shorter than half the lease; polling must be positive"
            )


DEFAULT_POLICY = LeasePolicy()


def clock(db: Session) -> datetime:
    source = (
        func.clock_timestamp()
        if db.get_bind().dialect.name == "postgresql"
        else func.current_timestamp()
    )
    value = db.scalar(select(source))
    assert isinstance(value, datetime)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def lease_for(db: Session, job_id: UUID) -> GenerationLease | None:
    return db.scalar(
        select(GenerationLease)
        .where(GenerationLease.job_id == job_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def owns(db: Session, lease: GenerationLease | None, token: UUID) -> bool:
    return (
        lease is not None
        and lease.token == token
        and lease.released_at is None
        and utc(lease.expires_at) > clock(db)
    )


def claim(
    db: Session, job: GenerationJob, attempt_id: UUID, policy: LeasePolicy
) -> GenerationLease | None:
    # Caller serializes project -> job -> lease, matching receipt/cancellation ordering.
    stamp = clock(db)
    lease = lease_for(db, job.id)
    if lease is not None and lease.released_at is None and utc(lease.expires_at) > stamp:
        return None
    token = uuid4()
    if lease is None:
        lease = GenerationLease(job_id=job.id, attempt_id=attempt_id, epoch=0)
        db.add(lease)
    lease.attempt_id = attempt_id
    lease.token = token
    lease.epoch += 1
    lease.owner = f"media:{token}"
    lease.heartbeat_at = stamp
    lease.expires_at = stamp + timedelta(seconds=policy.duration_seconds)
    lease.released_at = None
    db.flush()
    audit(
        db,
        job.project_id,
        lease.owner,
        "generation.execution_claimed",
        job.id,
        {"attempt_id": str(attempt_id), "epoch": lease.epoch, "token": str(token)},
    )
    return lease


def release(db: Session, lease: GenerationLease, policy: LeasePolicy) -> None:
    stamp = clock(db)
    lease.released_at = stamp
    lease.expires_at = max(utc(lease.heartbeat_at), stamp) + timedelta(seconds=policy.poll_seconds)
    db.flush()


def heartbeat(
    factory: sessionmaker[Session], job_id: UUID, token: UUID, policy: LeasePolicy
) -> bool:
    with factory.begin() as db:
        lease = lease_for(db, job_id)
        stamp = clock(db)
        if not owns(db, lease, token):
            return False
        assert lease is not None
        lease.heartbeat_at = stamp
        lease.expires_at = stamp + timedelta(seconds=policy.duration_seconds)
        return True


@contextmanager
def keep_alive(
    factory: sessionmaker[Session], job_id: UUID, token: UUID, policy: LeasePolicy
) -> Iterator[None]:
    stop = Event()

    def renew() -> None:
        while not stop.wait(policy.heartbeat_seconds):
            try:
                if not heartbeat(factory, job_id, token, policy):
                    return
            except SQLAlchemyError:
                # Failed renewal cannot confer execution rights. Final writes recheck the lease.
                return

    thread = Thread(target=renew, name=f"media-heartbeat-{job_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
