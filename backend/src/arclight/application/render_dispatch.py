"""Durable render intent survives lost messages and scans expired claims and cleanup."""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.generation_leases import clock
from arclight.persistence.models import RenderAttempt, RenderExecution, RenderJob


def candidates(factory: sessionmaker[Session]) -> list[UUID]:
    with factory() as db:
        cleanup = (
            select(RenderAttempt.id)
            .where(
                RenderAttempt.run_id == RenderJob.id,
                RenderAttempt.status.not_in(("RUNNING", "SUCCEEDED")),
                RenderAttempt.cleanup_status.in_(("FAILED", "PENDING")),
            )
            .exists()
        )
        return list(
            db.scalars(
                select(RenderJob.id)
                .join(RenderExecution, RenderExecution.id == RenderJob.id)
                .where(
                    or_(
                        RenderExecution.status == "QUEUED",
                        (RenderExecution.status == "RUNNING")
                        & (RenderJob.lease_expires_at <= clock(db)),
                        cleanup,
                    )
                )
                .order_by(RenderJob.updated_at, RenderJob.id)
                .limit(100)
            )
        )
