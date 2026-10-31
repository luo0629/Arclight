"""Durable media outbox and fenced, duplicate-safe receipt registration."""

from copy import deepcopy
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application import generation_leases as leases
from arclight.application import generation_scheduling as scheduling
from arclight.application.audit import audit
from arclight.application.generation_attempts import (
    current_attempt,
    finish_attempt,
    record_submission,
    set_provider_request,
    start_attempt,
)
from arclight.application.generation_reconciliation import mark_result_unknown
from arclight.application.generation_state import find_job, transition
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.persistence.base import utcnow
from arclight.persistence.generation import GenerationAttempt, GenerationDispatch
from arclight.providers.media_io import bounded_call
from arclight.providers.media_submission import (
    MediaSubmission,
    MediaSubmissionProvider,
    SubmissionReceipt,
    SubmissionUncertain,
)


def register_receipt(
    db: Session, job_id: UUID, attempt_id: UUID, receipt: SubmissionReceipt
) -> GenerationAttempt:
    # Revalidate adapter-created objects as well as deserialized messages.
    receipt = SubmissionReceipt.model_validate(receipt.model_dump(mode="json"))
    job = find_job(db, job_id, lock=True)
    attempt = db.get(GenerationAttempt, attempt_id, populate_existing=True)
    if attempt is None or attempt.job_id != job.id:
        raise DomainError("ATTEMPT_CONFLICT", "尝试不属于此媒体任务", 409)
    delivered = db.scalar(
        select(GenerationDispatch).where(
            GenerationDispatch.attempt_id == attempt.id,
            GenerationDispatch.status == "FINISHED",
        )
    )
    if delivered is not None and delivered.receipt_json == receipt.model_dump(mode="json"):
        # A delayed duplicate of the original delivery cannot regress a later query/outcome.
        return attempt
    result_json = receipt.result.model_dump(mode="json") if receipt.result else None
    if attempt.finished_at is not None:
        if (
            attempt.status != receipt.status
            or attempt.result_json != result_json
            or attempt.error_code != receipt.error_code
            or attempt.cost != receipt.cost
            or attempt.latency_ms != receipt.latency_ms
            or (
                receipt.request_id is not None and attempt.provider_request_id != receipt.request_id
            )
        ):
            raise DomainError("GENERATION_RECEIPT_CONFLICT", "历史尝试已有不同结果，不能覆盖", 409)
        return attempt
    current_attempt(db, job, attempt_id)
    if receipt.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return finish_attempt(
            db,
            job.id,
            attempt.id,
            Status(receipt.status),
            request_id=receipt.request_id,
            result=receipt.result,
            error_code=receipt.error_code,
            cost=receipt.cost,
            latency_ms=receipt.latency_ms,
        )
    if receipt.status == "RESULT_UNKNOWN":
        mark_result_unknown(db, job.id, attempt.id, request_id=receipt.request_id)
    elif receipt.status == "SUBMITTED":
        assert receipt.request_id is not None
        if attempt.status == "SUBMITTED" and attempt.provider_request_id == receipt.request_id:
            return attempt
        record_submission(db, job.id, attempt.id, receipt.request_id)
    else:
        set_provider_request(job, attempt, receipt.request_id)
        if attempt.status != "RUNNING":
            if job.status != "CANCEL_REQUESTED":
                transition(db, job.id, Status.RUNNING, job.revision)
            attempt.status = "RUNNING"
            db.flush()
    return attempt


def consume_dispatch(
    factory: sessionmaker[Session],
    job_id: UUID,
    event_id: UUID,
    provider: MediaSubmissionProvider,
    *,
    policy: leases.LeasePolicy = leases.DEFAULT_POLICY,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
) -> str:
    with factory.begin() as db:
        job = find_job(db, job_id, lock=True)
        event = db.get(GenerationDispatch, event_id, populate_existing=True, with_for_update=True)
        if (
            event is None
            or event.job_id != job.id
            or event.request_fingerprint != job.request_fingerprint
        ):
            raise DomainError("GENERATION_MESSAGE_CONFLICT", "媒体事件与任务不匹配", 409)
        if event.status != "READY":
            return job.status
        if job.status in {Status.CREATED, Status.RETRY_WAIT} and not scheduling.admit(
            db, job, execution_policy
        ):
            if job.status == Status.FAILED:
                event.status = "SKIPPED"
                event.finished_at = utcnow()
                event.receipt_json = {"error_code": "TIME_OR_ATTEMPT_LIMIT"}
            return job.status
        try:
            if provider.name != job.provider:
                raise DomainError("GENERATION_PROVIDER_MISMATCH", "供应商不匹配冻结输入", 409)
            if provider.capabilities.supports_idempotency != provider.supports_idempotency:
                raise DomainError(
                    "PROVIDER_CAPABILITY_CONFLICT", "Adapter idempotency declaration conflicts", 409
                )
            attempt = start_attempt(
                db, job.id, event.expected_revision, capabilities=provider.capabilities
            )
        except DomainError as exc:
            event.status = "SKIPPED"
            event.finished_at = utcnow()
            event.receipt_json = {"error_code": exc.code}
            if job.revision == event.expected_revision and job.status in {
                Status.CREATED,
                Status.RETRY_WAIT,
            }:
                transition(db, job.id, Status.FAILED, job.revision)
                scheduling.diagnose(db, job, "NOT_RETRYABLE", "INPUT_INVALID")
            return job.status
        event.status = "CLAIMED"
        event.attempt_id = attempt.id
        event.provider_supports_idempotency = provider.supports_idempotency
        event.provider_idempotency_key = (
            f"generation:{job.id}:{attempt.attempt_no}" if provider.supports_idempotency else None
        )
        lease = leases.claim(db, job, attempt.id, policy)
        if lease is None:
            raise DomainError("GENERATION_LEASE_BUSY", "媒体执行权尚未释放", 409)
        token = lease.token
        call = scheduling.reserve(db, job, attempt.id, "SUBMIT")
        call_id = call.id
        frozen_policy = scheduling.policy_for(db, job)
        schedule = scheduling.schedule_for(db, job)
        timeout = min(
            frozen_policy.call_timeout_seconds,
            (leases.utc(schedule.deadline_at) - leases.clock(db)).total_seconds(),
        )
        request = MediaSubmission(
            job.id,
            attempt.id,
            attempt.input_manifest_id,
            deepcopy(attempt.input_snapshot_json),
            event.provider_idempotency_key,
        )
        db.flush()

    # No connection or business lock is held during the generation call.
    with leases.keep_alive(factory, job_id, token, policy):
        try:
            raw = bounded_call(lambda: provider.submit(request), timeout)
            receipt = SubmissionReceipt.model_validate(raw.model_dump(mode="json"))
            if receipt.result is not None and len(
                receipt.result.output_handles
            ) != request.snapshot["context"]["generation"].get("batch_size", 1):
                raise SubmissionUncertain("Provider returned an inconsistent batch")
            if (
                receipt.status in {"SUBMITTED", "RUNNING"}
                and provider.capabilities.execution_mode == "SYNC"
            ):
                raise SubmissionUncertain("Synchronous adapter returned an asynchronous execution")
        except (TimeoutError, ConnectionError, SubmissionUncertain, ValidationError):
            receipt = SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")

    with factory.begin() as db:
        job = find_job(db, job_id, lock=True)
        event = db.get(GenerationDispatch, event_id, populate_existing=True, with_for_update=True)
        assert event is not None and event.attempt_id is not None
        lease = leases.lease_for(db, job.id)
        if not leases.owns(db, lease, token) or event.status != "CLAIMED":
            audit(
                db,
                job.project_id,
                "media-worker",
                "generation.execution_superseded",
                job.id,
                {
                    "event_id": str(event.id),
                    "token": str(token),
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
            return job.status
        assert lease is not None
        register_receipt(db, job.id, event.attempt_id, receipt)
        completed_call = db.get(scheduling.GenerationCall, call_id)
        assert completed_call is not None
        scheduling.complete_call(db, completed_call, receipt.status)
        if job.status == Status.FAILED:
            scheduling.retry_after_failure(db, job, receipt.error_code or "PROVIDER_FAILED")
        event.status = "FINISHED"
        event.receipt_json = receipt.model_dump(mode="json")
        event.finished_at = utcnow()
        leases.release(db, lease, policy)
        db.flush()
        return job.status
