"""Media outbox persistence and repeat publication until a consumer claims an event."""

from sqlalchemy.orm import Session, sessionmaker

from arclight.application.generation_queues import Publish, publish_ready
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.persistence.generation import GenerationDispatch, GenerationJob


def enqueue_dispatch(db: Session, job: GenerationJob) -> GenerationDispatch:
    row = GenerationDispatch(
        job_id=job.id,
        expected_revision=job.revision,
        request_fingerprint=job.request_fingerprint,
    )
    db.add(row)
    db.flush()
    from arclight.application.generation_queues import prepare_permit

    prepare_permit(db, job, row)
    return row


def publish_dispatches(
    factory: sessionmaker[Session],
    publish: Publish,
    live: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
) -> int:
    return publish_ready(factory, publish, live)
