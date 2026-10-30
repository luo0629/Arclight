"""Media outbox persistence and repeat publication until a consumer claims an event."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.persistence.generation import GenerationDispatch, GenerationJob


def enqueue_dispatch(db: Session, job: GenerationJob) -> GenerationDispatch:
    row = GenerationDispatch(
        job_id=job.id,
        expected_revision=job.revision,
        request_fingerprint=job.request_fingerprint,
    )
    db.add(row)
    db.flush()
    return row


def publish_dispatches(
    factory: sessionmaker[Session], publish: Callable[[UUID, UUID], None]
) -> int:
    with factory() as db:
        pending = list(
            db.execute(
                select(GenerationDispatch.job_id, GenerationDispatch.id)
                .where(GenerationDispatch.status == "READY")
                .order_by(GenerationDispatch.created_at, GenerationDispatch.id)
                .limit(100)
            )
        )
    # READY remains eligible after broker loss; provider I/O never runs in this transaction.
    for job_id, event_id in pending:
        publish(job_id, event_id)
    return len(pending)
