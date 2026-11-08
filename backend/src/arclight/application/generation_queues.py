"""Database-first fair admission. Broker deliveries never grant execution authority."""

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application import generation_leases as leases
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.persistence.generation import GenerationDispatch, GenerationJob
from arclight.persistence.generation_queues import (
    GenerationLane,
    GenerationPermit,
    GenerationPermitEvent,
    GenerationQueueState,
)
from arclight.persistence.generation_scheduling import (
    GenerationCall,
    GenerationSchedule,
    GenerationSchedulerGate,
)

QUEUES = {"INTERACTIVE": "generation.interactive", "BATCH": "generation.batch"}
Publish = Callable[[UUID, UUID, UUID, int, str], None]


def classify(
    db: Session, job: GenerationJob, payload: GenerationCreate | None = None
) -> GenerationLane:
    row = db.scalar(select(GenerationLane).where(GenerationLane.job_id == job.id))
    if row is None:
        lane, origin = "BATCH", "LEGACY_UNCLASSIFIED"
        if payload is not None:
            batch = payload.batch_id is not None or payload.spec.batch_size > 1
            lane = "BATCH" if batch else payload.scheduling_mode
            origin = "BULK_REQUEST" if batch else "SINGLE_REQUEST"
        row = GenerationLane(job_id=job.id, lane=lane, origin=origin, actor=job.created_by)
        db.add(row)
        db.flush()
    return row


def prepare_permit(db: Session, job: GenerationJob, event: GenerationDispatch) -> GenerationPermit:
    # Must run in the creation/retry transaction BEFORE acquiring the scheduler gate.
    # Even an INSERT's FK validation can otherwise invert project -> gate lock order.
    row = GenerationPermit(
        dispatch_id=event.id,
        job_id=job.id,
        project_id=job.project_id,
        provider=job.provider,
        actor=job.created_by,
        lane=classify(db, job).lane,
    )
    db.add(row)
    db.flush()
    return row


def lock_gate(db: Session) -> None:
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    assert db.get(GenerationSchedulerGate, "global", with_for_update=True) is not None


def record(db: Session, permit: GenerationPermit, action: str, reason: str | None = None) -> None:
    # Only this scheduling row is referenced here: no implicit business-row FK locks.
    sequence = (
        db.scalar(
            select(func.max(GenerationPermitEvent.sequence)).where(
                GenerationPermitEvent.permit_id == permit.id
            )
        )
        or 0
    )
    db.add(
        GenerationPermitEvent(
            permit_id=permit.id,
            sequence=sequence + 1,
            epoch=permit.epoch,
            action=action,
            reason=reason,
        )
    )
    db.flush()


def valid_permit(
    db: Session, event_id: UUID, permit_id: UUID, epoch: int
) -> GenerationPermit | None:
    row = db.get(GenerationPermit, permit_id, populate_existing=True)
    if (
        row is None
        or row.dispatch_id != event_id
        or row.epoch != epoch
        or row.state != "ISSUED"
        or row.expires_at is None
        or leases.utc(row.expires_at) <= leases.clock(db)
    ):
        return None
    return row


def revoke(db: Session, event_id: UUID, reason: str) -> None:
    lock_gate(db)
    row = db.scalar(select(GenerationPermit).where(GenerationPermit.dispatch_id == event_id))
    if row is not None and row.state in {"WAITING", "ISSUED"}:
        row.state, row.reason = "REVOKED", reason
        record(db, row, "REVOKED", reason)


def capacity_block(
    db: Session,
    job: GenerationJob,
    live: ExecutionPolicy,
    kind: str = "SUBMIT",
    *,
    lane: str = "INTERACTIVE",
    exclude_permit: UUID | None = None,
) -> str | None:
    """Under gate: count actual calls + unconsumed permits in the SAME shared quotas."""
    stamp = leases.clock(db)
    pending = (
        select(func.count())
        .select_from(GenerationPermit)
        .where(GenerationPermit.state == "ISSUED", GenerationPermit.expires_at > stamp)
    )
    if exclude_permit is not None:
        pending = pending.where(GenerationPermit.id != exclude_permit)
    active = (
        select(func.count())
        .select_from(GenerationCall)
        .where(GenerationCall.released_at.is_(None), GenerationCall.kind == kind)
    )
    provider_limit = (
        live.provider_limits.get(job.provider, live.provider_concurrency) if kind == "SUBMIT" else 4
    )
    project_limit = live.project_limits.get(str(job.project_id), live.project_concurrency)

    def count(statement) -> int:
        return db.scalar(statement) or 0

    if kind == "SUBMIT" and job.provider in live.paused_providers:
        return "PROVIDER_PAUSED"
    provider = count(active.where(GenerationCall.provider == job.provider))
    project = count(active.where(GenerationCall.project_id == job.project_id))
    if kind == "SUBMIT":
        provider += count(pending.where(GenerationPermit.provider == job.provider))
        project += count(pending.where(GenerationPermit.project_id == job.project_id))
    if provider >= provider_limit:
        return "PROVIDER_CONCURRENCY"
    if kind == "SUBMIT" and project >= project_limit:
        return "PROJECT_CONCURRENCY"
    if kind == "SUBMIT" and lane == "BATCH":
        batch_calls = active.join(
            GenerationLane, GenerationLane.job_id == GenerationCall.job_id
        ).where(GenerationLane.lane == "BATCH")
        batch_pending = pending.where(GenerationPermit.lane == "BATCH")
        reserve = live.interactive_concurrency_reserve
        if count(batch_calls.where(GenerationCall.provider == job.provider)) + count(
            batch_pending.where(GenerationPermit.provider == job.provider)
        ) >= max(1, provider_limit - reserve):
            return "INTERACTIVE_PROVIDER_RESERVE"
        if count(batch_calls.where(GenerationCall.project_id == job.project_id)) + count(
            batch_pending.where(GenerationPermit.project_id == job.project_id)
        ) >= max(1, project_limit - reserve):
            return "INTERACTIVE_PROJECT_RESERVE"
    recent = (
        select(func.count())
        .select_from(GenerationCall)
        .where(GenerationCall.started_at > stamp - timedelta(seconds=60))
    )
    for total, scope, call_filter, permit_filter in (
        (live.global_per_minute, "GLOBAL", None, None),
        (
            live.user_per_minute,
            "USER",
            GenerationCall.actor == job.created_by,
            GenerationPermit.actor == job.created_by,
        ),
    ):
        calls = recent.where(call_filter) if call_filter is not None else recent
        tickets = pending.where(permit_filter) if permit_filter is not None else pending
        usage = count(calls) + count(tickets)
        if usage >= total:
            return f"{scope}_RATE"
        if kind == "SUBMIT":
            submit_ceiling = max(1, total - live.recovery_rate_reserve)
            if usage >= submit_ceiling:
                return f"{scope}_RECOVERY_RESERVE"
            if lane == "BATCH":
                batch_calls = calls.join(
                    GenerationLane, GenerationLane.job_id == GenerationCall.job_id
                ).where(GenerationCall.kind == "SUBMIT", GenerationLane.lane == "BATCH")
                batch_usage = count(batch_calls) + count(
                    tickets.where(GenerationPermit.lane == "BATCH")
                )
                if batch_usage >= max(1, submit_ceiling - live.interactive_rate_reserve):
                    return f"{scope}_INTERACTIVE_RESERVE"
    return None


def _reclaim(db: Session) -> None:
    stamp = leases.clock(db)
    rows = db.execute(
        select(
            GenerationPermit,
            GenerationDispatch.status,
            GenerationJob.status,
            GenerationJob.revision,
            GenerationDispatch.expected_revision,
        )
        .join(GenerationDispatch, GenerationDispatch.id == GenerationPermit.dispatch_id)
        .join(GenerationJob, GenerationJob.id == GenerationPermit.job_id)
        .where(
            GenerationPermit.state.in_(["WAITING", "ISSUED"]),
            or_(
                GenerationDispatch.status != "READY",
                GenerationJob.status.not_in(["CREATED", "RETRY_WAIT"]),
                GenerationJob.revision != GenerationDispatch.expected_revision,
                (GenerationPermit.state == "ISSUED") & (GenerationPermit.expires_at <= stamp),
            ),
        )
        .order_by(GenerationPermit.updated_at, GenerationPermit.id)
        .limit(1000)
    )
    for permit, event_status, job_status, revision, expected in rows:
        if (
            event_status != "READY"
            or job_status not in {"CREATED", "RETRY_WAIT"}
            or revision != expected
        ):
            permit.state, permit.reason = "REVOKED", "NOT_SUBMITTABLE"
            record(db, permit, "REVOKED", permit.reason)
        elif permit.state == "ISSUED" and leases.utc(permit.expires_at) <= stamp:
            permit.state, permit.reason = "WAITING", "PERMIT_EXPIRED"
            record(db, permit, "EXPIRED", permit.reason)


def _candidate_page(db: Session, lane: str, cursor: str):
    stamp = leases.clock(db)
    # One FIFO head per project/provider; a paused provider cannot hide a different
    # provider in that project. Keyset paging persists progress even when all heads block.
    ranked = (
        select(
            GenerationPermit.id.label("permit_id"),
            cast(GenerationPermit.project_id, String).label("group_key"),
            GenerationSchedule.next_attempt_at.label("ready_at"),
            GenerationPermit.created_at.label("queued_at"),
            func.row_number()
            .over(
                partition_by=(GenerationPermit.project_id, GenerationPermit.provider),
                order_by=(
                    GenerationSchedule.next_attempt_at,
                    GenerationPermit.created_at,
                    GenerationPermit.id,
                ),
            )
            .label("position"),
        )
        .join(GenerationSchedule, GenerationSchedule.job_id == GenerationPermit.job_id)
        .join(GenerationDispatch, GenerationDispatch.id == GenerationPermit.dispatch_id)
        .join(GenerationJob, GenerationJob.id == GenerationPermit.job_id)
        .where(
            GenerationPermit.lane == lane,
            GenerationPermit.state == "WAITING",
            GenerationSchedule.next_attempt_at <= stamp,
            or_(GenerationPermit.publish_after.is_(None), GenerationPermit.publish_after <= stamp),
            GenerationDispatch.status == "READY",
            GenerationJob.status.in_(["CREATED", "RETRY_WAIT"]),
            GenerationJob.revision == GenerationDispatch.expected_revision,
        )
        .subquery()
    )
    query = select(ranked.c.permit_id, ranked.c.group_key).where(ranked.c.position == 1)
    rows = list(
        db.execute(
            query.where(ranked.c.group_key > cursor)
            .order_by(ranked.c.group_key, ranked.c.ready_at, ranked.c.queued_at, ranked.c.permit_id)
            .limit(256)
        )
    )
    if not rows and cursor:
        rows = list(
            db.execute(
                query.order_by(
                    ranked.c.group_key, ranked.c.ready_at, ranked.c.queued_at, ranked.c.permit_id
                ).limit(256)
            )
        )
    return rows


def eligibility_block(db: Session, job: GenerationJob) -> str | None:
    """Advisory read only checks under gate. Worker repeats authoritative locked checks."""
    from pydantic import ValidationError

    from arclight.application.commands import require_project
    from arclight.application.generation_attempts import require_writable_target
    from arclight.application.generation_budget import check_execution
    from arclight.core.errors import DomainError
    from arclight.domain.generation_requests import GenerationSpec
    from arclight.persistence.models import DependencyManifest, ProjectMember, User

    try:
        # write=False deliberately avoids project locks while holding the global gate.
        project = require_project(db, job.project_id, job.created_by)
        if project.archived:
            return "PROJECT_ARCHIVED"
        if job.created_by != "service-owner":
            user = db.get(User, UUID(job.created_by))
            if user is None or user.disabled:
                return "CREATOR_DISABLED"
            if project.owner_id != job.created_by:
                role = db.scalar(
                    select(ProjectMember.role).where(
                        ProjectMember.project_id == job.project_id,
                        ProjectMember.user_id == UUID(job.created_by),
                    )
                )
                if role != "editor":
                    return "FORBIDDEN"
        require_writable_target(db, job)
        manifest = db.get(DependencyManifest, job.input_manifest_id)
        if manifest is None:
            return "INVALID_GENERATION_INPUT"
        spec = GenerationSpec.model_validate(manifest.snapshot_json["context"]["generation"])
        check_execution(db, job, spec, lock=False)
    except DomainError as exc:
        return exc.code
    except (KeyError, TypeError, ValidationError):
        return "INVALID_GENERATION_INPUT"
    return None


def issue_permits(
    factory: sessionmaker[Session], live: ExecutionPolicy = DEFAULT_EXECUTION_POLICY
) -> int:
    expire_waiting(factory)
    # Commit reclamation before reissuing the same permit. A second UPDATE in one
    # PostgreSQL transaction rechecks its business-row foreign keys, which can wait
    # on a consumer holding those rows while waiting for this scheduling gate.
    with factory.begin() as db:
        lock_gate(db)
        _reclaim(db)
    issued = 0
    with factory.begin() as db:
        lock_gate(db)
        state = db.get(GenerationQueueState, "global", populate_existing=True)
        assert state is not None
        state.heartbeat_at = leases.clock(db)
        pending_count = (
            db.scalar(
                select(func.count())
                .select_from(GenerationPermit)
                .where(
                    GenerationPermit.state == "ISSUED",
                    GenerationPermit.expires_at > leases.clock(db),
                )
            )
            or 0
        )
        cycle = ["INTERACTIVE"] * live.interactive_weight + ["BATCH"] * live.batch_weight
        blocked_scopes: dict[tuple[str, str, str], str] = {}
        examined = 0
        for _ in range(max(0, live.dispatch_window - pending_count)):
            selected = False
            for step in range(len(cycle)):
                turn = (state.turn + step) % len(cycle)
                lane = cycle[turn]
                attribute = "interactive_cursor" if lane == "INTERACTIVE" else "batch_cursor"
                for permit_id, group_key in _candidate_page(db, lane, getattr(state, attribute)):
                    if examined >= 1024:
                        break
                    examined += 1
                    setattr(state, attribute, group_key)
                    permit = db.get(GenerationPermit, permit_id)
                    assert permit is not None
                    job = db.get(GenerationJob, permit.job_id)
                    assert job is not None
                    scopes = [
                        ("GLOBAL", "", lane),
                        ("PROVIDER", job.provider, lane),
                        ("PROJECT", str(job.project_id), lane),
                        ("USER", job.created_by, lane),
                    ]
                    block = next(
                        (blocked_scopes[key] for key in scopes if key in blocked_scopes), None
                    )
                    if block is None:
                        block = capacity_block(db, job, live, lane=lane)
                    if block:
                        permit.reason = block
                        scope = next((key for key in scopes if key[0] in block), None)
                        if scope is not None:
                            blocked_scopes[scope] = block
                        continue
                    block = eligibility_block(db, job)
                    if block:
                        permit.reason = block
                        permit.publish_after = leases.clock(db) + timedelta(seconds=5)
                        continue
                    permit.reason = None
                    stamp = leases.clock(db)
                    permit.state = "ISSUED"
                    permit.epoch += 1
                    permit.issued_at = stamp
                    permit.expires_at = stamp + timedelta(seconds=live.permit_seconds)
                    permit.publish_after = stamp
                    record(db, permit, "ISSUED")
                    state.turn = (turn + 1) % len(cycle)
                    issued += 1
                    selected = True
                    break
                if selected:
                    break
            if not selected:
                break
    return issued


def expire_waiting(factory: sessionmaker[Session]) -> None:
    """Deadline cleanup acquires business locks BEFORE gate, including paused queues."""
    from arclight.application.generation_scheduling import within_limits
    from arclight.application.generation_state import find_job

    with factory() as db:
        ids = list(
            db.scalars(
                select(GenerationJob.id)
                .join(GenerationSchedule, GenerationSchedule.job_id == GenerationJob.id)
                .where(
                    GenerationJob.status.in_(["CREATED", "RETRY_WAIT"]),
                    GenerationSchedule.deadline_at <= leases.clock(db),
                )
                .order_by(GenerationSchedule.deadline_at, GenerationJob.id)
                .limit(100)
            )
        )
    for job_id in ids:
        with factory.begin() as db:
            job = find_job(db, job_id, lock=True)
            if job.status in {"CREATED", "RETRY_WAIT"}:
                within_limits(db, job, submitting=True)


def claim_recovery_scan(factory: sessionmaker[Session]) -> bool:
    with factory.begin() as db:
        lock_gate(db)
        state = db.get(GenerationQueueState, "global", populate_existing=True)
        assert state is not None
        stamp = leases.clock(db)
        if (
            state.recovery_publish_after is not None
            and leases.utc(state.recovery_publish_after) > stamp
        ):
            return False
        state.recovery_publish_after = stamp + timedelta(seconds=10)
        return True


def publish_ready(
    factory: sessionmaker[Session],
    publish: Publish,
    live: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
) -> int:
    issue_permits(factory, live)
    # Reserve a publication window before broker I/O. A crash/failure is retried after
    # cooldown with the same permit+epoch; publication never holds a database lock.
    with factory.begin() as db:
        lock_gate(db)
        stamp = leases.clock(db)
        rows = list(
            db.scalars(
                select(GenerationPermit)
                .where(
                    GenerationPermit.state == "ISSUED",
                    GenerationPermit.expires_at > stamp,
                    GenerationPermit.publish_after <= stamp,
                )
                .order_by(GenerationPermit.issued_at, GenerationPermit.id)
                .limit(live.dispatch_window)
            )
        )
        messages = []
        for permit in rows:
            permit.publish_count += 1
            permit.publish_after = stamp + timedelta(seconds=live.publish_cooldown_seconds)
            record(db, permit, "PUBLISH")
            messages.append(
                (permit.job_id, permit.dispatch_id, permit.id, permit.epoch, permit.lane)
            )
    for message in messages:
        publish(*message)
    return len(messages)
