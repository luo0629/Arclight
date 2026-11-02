"""Project-scoped business backlog; never confuse job counts with broker messages."""

from math import ceil
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from arclight.application import generation_leases as leases
from arclight.persistence.generation import GenerationDispatch, GenerationJob
from arclight.persistence.generation_queues import (
    GenerationLane,
    GenerationPermit,
    GenerationQueueState,
)
from arclight.persistence.generation_scheduling import GenerationDeadLetter, GenerationSchedule


def queue_overview(db: Session, project_id: UUID) -> dict:
    stamp = leases.clock(db)
    latest = (
        select(
            GenerationDispatch.job_id,
            func.max(GenerationDispatch.expected_revision).label("revision"),
        )
        .group_by(GenerationDispatch.job_id)
        .subquery()
    )
    query = (
        select(
            GenerationLane.lane,
            GenerationLane.created_at.label("queued_at"),
            GenerationJob.status,
            GenerationSchedule.next_attempt_at,
            GenerationPermit.state,
            GenerationPermit.expires_at,
            func.coalesce(GenerationPermit.reason, GenerationSchedule.blocked_code).label("reason"),
            GenerationDeadLetter.id.label("dead_letter_id"),
        )
        .select_from(GenerationJob)
        .join(GenerationLane, GenerationLane.job_id == GenerationJob.id)
        .join(GenerationSchedule, GenerationSchedule.job_id == GenerationJob.id)
        .outerjoin(GenerationDeadLetter, GenerationDeadLetter.job_id == GenerationJob.id)
        .outerjoin(latest, latest.c.job_id == GenerationJob.id)
        .outerjoin(
            GenerationDispatch,
            (GenerationDispatch.job_id == latest.c.job_id)
            & (GenerationDispatch.expected_revision == latest.c.revision),
        )
        .outerjoin(GenerationPermit, GenerationPermit.dispatch_id == GenerationDispatch.id)
        .where(GenerationJob.project_id == project_id)
    ).subquery()
    pending = query.c.status.in_(["CREATED", "RETRY_WAIT"])
    issued = (query.c.state == "ISSUED") & (query.c.expires_at > stamp)
    backoff = query.c.next_attempt_at > stamp
    categories = {
        "ready": pending & ~issued & ~backoff,
        "backoff": pending & ~issued & backoff,
        "waiting_permit": pending & issued,
        "executing": query.c.status.in_(["SUBMITTING", "SUBMITTED", "RUNNING", "CANCEL_REQUESTED"]),
        "unknown": query.c.status.in_(["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"]),
        "terminal": query.c.status.in_(["SUCCEEDED", "FAILED", "CANCELLED"]),
        "dead_letter": query.c.dead_letter_id.is_not(None),
    }
    lanes = []
    for lane in ("INTERACTIVE", "BATCH"):
        counts = (
            db.execute(
                select(
                    *[
                        func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)
                        for name, condition in categories.items()
                    ]
                ).where(query.c.lane == lane)
            )
            .one()
            ._asdict()
        )
        waits = (
            select(query.c.queued_at)
            .where(pending, query.c.lane == lane)
            .order_by(query.c.queued_at)
        )
        count = counts["ready"] + counts["backoff"] + counts["waiting_permit"]
        oldest = db.scalar(waits.limit(1))
        percentiles = {}
        for label, fraction in (("p50", 0.5), ("p95", 0.95)):
            value = db.scalar(waits.offset(max(0, count - ceil(count * fraction))).limit(1))
            percentiles[label] = (
                max(0, (stamp - leases.utc(value)).total_seconds()) if value else None
            )
        reasons = db.execute(
            select(query.c.reason, func.count())
            .where(query.c.lane == lane, pending, query.c.reason.is_not(None))
            .group_by(query.c.reason)
        )
        lanes.append(
            {
                "lane": lane,
                **counts,
                "oldest_waiting_at": leases.utc(oldest) if oldest else None,
                "oldest_wait_seconds": max(0, (stamp - leases.utc(oldest)).total_seconds())
                if oldest
                else None,
                "wait_seconds": percentiles,
                "blocked_reasons": {reason: count for reason, count in reasons},
            }
        )
    state = db.get(GenerationQueueState, "global")
    heartbeat = leases.utc(state.heartbeat_at) if state and state.heartbeat_at else None
    return {
        "observed_at": stamp,
        "scheduler_heartbeat_at": heartbeat,
        "scheduler_stale": heartbeat is None or (stamp - heartbeat).total_seconds() > 30,
        "count_unit": "business_jobs",
        "broker_message_count": None,
        "policy_version": 1,
        "lanes": lanes,
    }
