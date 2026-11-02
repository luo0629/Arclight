"""Short transactions serialize media state changes; external calls never live here."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.core.errors import DomainError
from arclight.domain.generation import UNCERTAIN_STATUSES, GenerationStatus, validate_transition
from arclight.persistence.base import utcnow
from arclight.persistence.models import GenerationJob, Project


def find_job(db: Session, job_id: UUID, *, lock: bool = False) -> GenerationJob:
    if lock:
        connection = db.connection()
        if connection.dialect.name == "sqlite":
            driver = connection.connection.driver_connection
            assert driver is not None
            if not driver.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        identity = db.get(GenerationJob, job_id)
        if identity is None:
            raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
        # Match API project -> job ordering; result/audit foreign keys take key-share locks.
        db.get(Project, identity.project_id, with_for_update={"key_share": True})
    row = db.get(
        GenerationJob,
        job_id,
        populate_existing=True,
        with_for_update={"key_share": True} if lock else None,
    )
    if row is None:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    return row


def check_revision(row: GenerationJob, expected_revision: int) -> None:
    if row.revision != expected_revision:
        raise DomainError("VERSION_CONFLICT", "媒体任务状态已变化，请刷新后重试", 409)


def transition(
    db: Session,
    job_id: UUID,
    target: GenerationStatus,
    expected_revision: int,
    *,
    actor: str = "media-worker",
) -> GenerationJob:
    row = find_job(db, job_id, lock=True)
    check_revision(row, expected_revision)
    validate_transition(row.status, target)
    previous = row.status
    row.status = target
    row.revision += 1
    db.flush()
    if previous in {GenerationStatus.CREATED, GenerationStatus.RETRY_WAIT} and target in {
        GenerationStatus.CANCELLED,
        GenerationStatus.FAILED,
    }:
        from arclight.application.generation_accounting import release_unsubmitted

        release_unsubmitted(db, row)
        from sqlalchemy import select

        from arclight.application.generation_queues import revoke
        from arclight.persistence.generation import GenerationDispatch

        for event_id in db.scalars(
            select(GenerationDispatch.id).where(
                GenerationDispatch.job_id == row.id, GenerationDispatch.status == "READY"
            )
        ):
            revoke(db, event_id, str(target))
    audit(
        db,
        row.project_id,
        actor,
        "generation.transitioned",
        row.id,
        {
            "from": previous,
            "to": target,
            "revision": row.revision,
        },
    )
    return row


def request_cancel(
    db: Session,
    job_id: UUID,
    expected_revision: int,
    actor: str,
) -> GenerationJob:
    row = find_job(db, job_id, lock=True)
    check_revision(row, expected_revision)
    if row.status == GenerationStatus.SUCCEEDED:
        raise DomainError("GENERATION_FINISHED", "已成功的媒体任务不能取消", 409)
    if row.cancel_requested_at is not None or row.status == GenerationStatus.CANCELLED:
        return row
    stamp = utcnow()
    if row.status in UNCERTAIN_STATUSES or row.status == GenerationStatus.CANCEL_REQUESTED:
        # The provider may already be charging. Intent must not claim cancellation.
        row.revision += 1
    else:
        target = (
            GenerationStatus.CANCELLED
            if row.status
            in {GenerationStatus.CREATED, GenerationStatus.RETRY_WAIT, GenerationStatus.FAILED}
            else GenerationStatus.CANCEL_REQUESTED
        )
        row = transition(db, row.id, target, expected_revision, actor=actor)
    row.cancel_requested_at = stamp
    audit(
        db,
        row.project_id,
        actor,
        "generation.cancel_requested",
        row.id,
        {
            "revision": row.revision,
            "status": row.status,
        },
    )
    db.flush()
    return row
