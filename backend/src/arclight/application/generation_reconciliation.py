"""Durable query facts with short transactions on either side of provider I/O."""

from time import monotonic
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application import generation_leases as leases
from arclight.application import generation_scheduling as scheduling
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_attempts import (
    current_attempt,
    finish_attempt,
    set_provider_request,
)
from arclight.application.generation_state import check_revision, find_job, transition
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_requests import MediaErrorCode, MediaInput
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.persistence.base import utcnow
from arclight.persistence.generation import (
    GenerationAttempt,
    GenerationJob,
    GenerationReconciliation,
)
from arclight.providers.media_cancel import CancellationReceipt, MediaCancelProvider
from arclight.providers.media_io import bounded_call
from arclight.providers.media_query import (
    MediaQueryProvider,
    ProviderLookup,
    ProviderQueryError,
    QueryReceipt,
)


class ManualResolution(MediaInput):
    expected_revision: int = Field(strict=True, ge=0)
    evidence_reference: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")
    confirmed: bool = Field(strict=True)
    receipt: QueryReceipt


def mark_result_unknown(
    db: Session, job_id: UUID, attempt_id: UUID, *, request_id: str | None = None
) -> GenerationJob:
    """Use after an ambiguous submission/timeout, never a definite failure."""
    job = find_job(db, job_id, lock=True)
    attempt = current_attempt(db, job, attempt_id)
    if attempt.finished_at is not None:
        raise DomainError("ATTEMPT_FINISHED", "历史尝试不能覆盖", 409)
    set_provider_request(job, attempt, request_id)
    if job.status != Status.RESULT_UNKNOWN:
        transition(db, job.id, Status.RESULT_UNKNOWN, job.revision)
    attempt.status = Status.RESULT_UNKNOWN
    attempt.error_code = "RESULT_UNKNOWN"
    db.flush()
    return job


def reconcile(
    factory: sessionmaker[Session],
    project_id: UUID,
    job_id: UUID,
    expected_revision: int,
    actor: str,
    provider: MediaQueryProvider,
    *,
    manual: ManualResolution | None = None,
    lease_token: UUID | None = None,
    lease_policy: leases.LeasePolicy = leases.DEFAULT_POLICY,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    cancel_provider: MediaCancelProvider | None = None,
) -> GenerationJob:
    # The auth dependency's read transaction must already have ended in the HTTP caller.
    with factory.begin() as db:
        if lease_token is None:
            require_project(db, project_id, actor, write=True, owner=manual is not None)
        job = find_job(db, job_id, lock=True)
        if job.project_id != project_id:
            raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
        check_revision(job, expected_revision)
        if lease_token is not None and not leases.owns(
            db, leases.lease_for(db, job.id), lease_token
        ):
            raise DomainError("GENERATION_LEASE_LOST", "媒体执行权已失效", 409)
        if job.status not in {Status.RESULT_UNKNOWN, Status.RECONCILING, Status.MANUAL_REVIEW}:
            raise DomainError(
                "GENERATION_NOT_RECONCILABLE", "仅未知结果或人工处理中的任务可以对账", 409
            )
        if manual is not None and (
            not manual.confirmed
            or manual.receipt.status
            not in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_ACCEPTED"}
            or "://" in manual.evidence_reference
        ):
            raise DomainError(
                "INVALID_RECONCILIATION_EVIDENCE",
                "人工处理须确认明确证据，不能以查询不到代替未接收",
                422,
            )
        attempt = db.scalar(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == job.id,
                GenerationAttempt.attempt_no == job.attempt_count,
            )
        )
        if attempt is None or attempt.finished_at is not None:
            raise DomainError("ATTEMPT_CONFLICT", "缺少未完成的当前尝试", 409)
        schedule = scheduling.schedule_for(db, job)
        if lease_token is not None and leases.clock(db) >= leases.utc(schedule.deadline_at):
            if job.status != Status.RECONCILING:
                transition(db, job.id, Status.RECONCILING, job.revision, actor=actor)
            transition(db, job.id, Status.MANUAL_REVIEW, job.revision, actor=actor)
            scheduling.diagnose(db, job, "TIME_LIMIT", "RESULT_UNKNOWN")
            held = leases.lease_for(db, job.id)
            if held is not None:
                leases.release(db, held, lease_policy)
            return job
        query_call_id = cancel_call_id = None
        timeout = scheduling.policy_for(db, job).call_timeout_seconds
        if lease_token is not None:
            timeout = min(
                timeout, (leases.utc(schedule.deadline_at) - leases.clock(db)).total_seconds()
            )
        if manual is None and attempt.provider_request_id:
            if not scheduling.admit(db, job, execution_policy, "QUERY"):
                return job
            query_call_id = scheduling.reserve(db, job, attempt.id, "QUERY").id
            cancelled_before = db.scalar(
                select(scheduling.GenerationCall.id).where(
                    scheduling.GenerationCall.attempt_id == attempt.id,
                    scheduling.GenerationCall.kind == "CANCEL",
                )
            )
            if (
                job.cancel_requested_at is not None
                and cancel_provider is not None
                and cancelled_before is None
                and scheduling.admit(db, job, execution_policy, "CANCEL")
            ):
                cancel_call_id = scheduling.reserve(db, job, attempt.id, "CANCEL").id
        if job.status == Status.RECONCILING:
            # A crashed or superseded read-only query may be replaced, never resubmitted.
            job.revision += 1
            db.flush()
        else:
            transition(db, job.id, Status.RECONCILING, job.revision, actor=actor)
        fact = GenerationReconciliation(
            job_id=job.id,
            attempt_id=attempt.id,
            claimed_revision=job.revision,
            source="MANUAL" if manual else "PROVIDER",
            actor=actor,
            evidence_reference=manual.evidence_reference if manual else None,
        )
        db.add(fact)
        db.flush()
        fact_id = fact.id
        lookup = (
            ProviderLookup(attempt.provider, attempt.model, attempt.provider_request_id)
            if attempt.provider_request_id
            else None
        )
        audit(
            db,
            project_id,
            actor,
            "generation.reconciliation_started",
            job.id,
            {"reconciliation_id": str(fact_id), "source": fact.source},
        )

    # No Session/connection or business row lock spans this read-only external call.
    io_deadline = monotonic() + timeout
    cancellation = None
    if cancel_call_id is not None and cancel_provider is not None and lookup is not None:
        try:
            cancellation = CancellationReceipt.model_validate(
                bounded_call(lambda: cancel_provider.cancel(lookup), io_deadline - monotonic())
            )
        except (TimeoutError, ConnectionError, ValidationError):
            cancellation = CancellationReceipt(status="RESULT_UNKNOWN")
    if cancellation is not None and cancellation.status == "CONFIRMED":
        receipt = QueryReceipt(status="CANCELLED")
    elif manual is not None:
        receipt = manual.receipt
    elif lookup is None:
        receipt = QueryReceipt(status="UNSUPPORTED")
    else:
        try:
            receipt = QueryReceipt.model_validate(
                bounded_call(lambda: provider.query(lookup), io_deadline - monotonic())
            )
        except (TimeoutError, ConnectionError, ProviderQueryError):
            receipt = QueryReceipt(status="QUERY_FAILED")
        except ValidationError:
            receipt = QueryReceipt(status="INVALID_RESPONSE")

    with factory.begin() as db:
        job = find_job(db, job_id, lock=True)
        completed_fact = db.get(GenerationReconciliation, fact_id)
        assert completed_fact is not None
        fact = completed_fact
        assert fact is not None
        attempt = db.get(GenerationAttempt, fact.attempt_id)
        assert attempt is not None
        for call_id, outcome in (
            (query_call_id, receipt.status),
            (cancel_call_id, cancellation.status if cancellation else None),
        ):
            if call_id is not None:
                call = db.get(scheduling.GenerationCall, call_id)
                assert call is not None
                scheduling.complete_call(db, call, outcome or "RESULT_UNKNOWN")
        applicable = (
            job.status == Status.RECONCILING
            and job.revision == fact.claimed_revision
            and attempt.finished_at is None
            and attempt.attempt_no == job.attempt_count
        )
        lease = leases.lease_for(db, job.id) if lease_token is not None else None
        if lease_token is not None:
            applicable = applicable and leases.owns(db, lease, lease_token)
        fact.receipt_json = receipt.model_dump(mode="json")
        fact.disposition = "APPLIED" if applicable else "SUPERSEDED"
        fact.finished_at = utcnow()
        if applicable:
            if receipt.status in {"SUCCEEDED", "FAILED", "CANCELLED", "NOT_ACCEPTED"}:
                target = (
                    Status.FAILED if receipt.status == "NOT_ACCEPTED" else Status(receipt.status)
                )
                error: MediaErrorCode | None = (
                    None
                    if target == Status.SUCCEEDED
                    else (
                        "CANCELLED"
                        if target == Status.CANCELLED
                        else (receipt.error_code or "PROVIDER_FAILED")
                    )
                )
                finish_attempt(
                    db,
                    job.id,
                    attempt.id,
                    target,
                    result=receipt.result,
                    error_code=error,
                    latency_ms=receipt.latency_ms,
                    cost=receipt.cost,
                )
                if receipt.status == "NOT_ACCEPTED":
                    if job.cancel_requested_at:
                        transition(db, job.id, Status.CANCELLED, job.revision, actor=actor)
                    else:
                        scheduling.retry_after_failure(
                            db, job, "PROVIDER_FAILED", not_accepted=True, actor=actor
                        )
                elif target == Status.FAILED:
                    scheduling.retry_after_failure(db, job, error or "PROVIDER_FAILED")
            elif receipt.status == "RUNNING":
                transition(
                    db,
                    job.id,
                    Status.CANCEL_REQUESTED if job.cancel_requested_at else Status.RUNNING,
                    job.revision,
                    actor=actor,
                )
                attempt.status = Status.RUNNING
                if leases.clock(db) >= leases.utc(scheduling.schedule_for(db, job).deadline_at):
                    mark_result_unknown(db, job.id, attempt.id)
                    transition(db, job.id, Status.RECONCILING, job.revision, actor=actor)
                    transition(db, job.id, Status.MANUAL_REVIEW, job.revision, actor=actor)
                    scheduling.diagnose(db, job, "TIME_LIMIT", "RESULT_UNKNOWN")
            else:
                transition(db, job.id, Status.MANUAL_REVIEW, job.revision, actor=actor)
                attempt.status = Status.RESULT_UNKNOWN
                scheduling.diagnose(db, job, "RESULT_UNKNOWN", "RESULT_UNKNOWN")
            db.flush()
        audit(
            db,
            project_id,
            actor,
            "generation.reconciliation_finished",
            job.id,
            {
                "reconciliation_id": str(fact_id),
                "disposition": fact.disposition,
                "outcome": receipt.status,
            },
        )
        if lease is not None and lease_token is not None and leases.owns(db, lease, lease_token):
            leases.release(db, lease, lease_policy)
        db.flush()
        return job
