"""Shared project totals, reservation admission, and authoritative reconciliation."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayRequest, GatewayRoute, GatewayUsage
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt


def latest_receipt(db: Session, call_id: UUID) -> GatewayReceipt | None:
    return db.scalar(
        select(GatewayReceipt)
        .where(GatewayReceipt.call_id == call_id)
        .order_by(GatewayReceipt.kind.desc())
        .limit(1)
    )


def media_outcome(db: Session, call: GatewayCall) -> dict | None:
    """Read GEN's authoritative outcome and billing, without a second media ledger."""
    if call.generation_attempt_id is None:
        return None
    from arclight.application.generation_accounting import reservation_for
    from arclight.persistence.generation import GenerationAttempt
    from arclight.persistence.generation_accounting import GenerationBudgetSettlement

    attempt = db.get(GenerationAttempt, call.generation_attempt_id)
    assert attempt is not None
    reservation = reservation_for(db, attempt.job_id, attempt.attempt_no)
    settlement = (
        db.scalar(
            select(GenerationBudgetSettlement).where(
                GenerationBudgetSettlement.reservation_id == reservation.id
            )
        )
        if reservation
        else None
    )
    usage = GatewayUsage(
        images=len(attempt.result_json["output_handles"])
        if attempt.result_json and call.task == "IMAGE"
        else None,
        actual_cost=settlement.cost if settlement else attempt.cost,
    )
    return {
        "status": attempt.status
        if attempt.status
        in {"SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "RESULT_UNKNOWN"}
        else "RESULT_UNKNOWN",
        "error_code": attempt.error_code,
        "usage": usage.model_dump(mode="json"),
        "cost_is_estimate": usage.actual_cost is None,
        "outcome_source": "MEDIA_ATTEMPT",
    }


def costs(
    db: Session, project_id: UUID, *, episode_id: UUID | None = None
) -> tuple[Decimal, Decimal]:
    statement = select(GatewayCall).where(
        GatewayCall.project_id == project_id, GatewayCall.generation_attempt_id.is_(None)
    )
    if episode_id is not None:
        statement = statement.where(GatewayCall.episode_id == episode_id)
    actual = pending = Decimal("0")
    for call in db.scalars(statement):
        receipt = latest_receipt(db, call.id)
        if receipt is not None and receipt.actual_cost is not None:
            actual += receipt.actual_cost
        elif call.maximum_cost is not None:
            pending += call.maximum_cost
    return actual, pending


def admit_budget(db: Session, request: GatewayRequest, route: GatewayRoute) -> None:
    from arclight.application.generation_accounting import costs as project_costs
    from arclight.application.generation_budget import policy_for
    from arclight.persistence.generation import GenerationAttempt, GenerationJob
    from arclight.persistence.generation_accounting import GenerationBudgetReservation
    from arclight.persistence.models import Project

    if request.generation_attempt_id is not None:
        if route.maximum_cost is None:
            raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "媒体调用缺少可信报价，已停止调用", 409)
        attempt = db.get(GenerationAttempt, request.generation_attempt_id)
        job = db.get(GenerationJob, attempt.job_id) if attempt else None
        reservation = (
            db.scalar(
                select(GenerationBudgetReservation)
                .where(
                    GenerationBudgetReservation.job_id == attempt.job_id,
                    GenerationBudgetReservation.attempt_no == attempt.attempt_no,
                )
                .order_by(GenerationBudgetReservation.sequence.desc())
                .limit(1)
            )
            if attempt
            else None
        )
        if (
            attempt is None
            or job is None
            or job.project_id != request.project_id
            or job.created_by != request.actor
            or attempt.finished_at is not None
            or job.attempt_count != attempt.attempt_no
            or reservation is None
            or reservation.amount != route.maximum_cost
            or job.provider != route.provider
            or job.model != route.model
        ):
            raise DomainError("BUDGET_RESERVATION_CONFLICT", "媒体调用与现有预算预留不匹配", 409)
        return
    # Personal text services are billed by the upstream account. An unknown
    # price is recorded as unknown and does not require a local budget quote.
    if route.maximum_cost is None:
        if request.task not in {"script", "storyboard", "analyze", "outline", "rewrite"}:
            raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "媒体调用缺少可信报价，已停止调用", 409)
        return
    project = db.get(Project, request.project_id)
    assert project is not None
    policy = policy_for(db, project.id)
    actual, pending = project_costs(db, project.id)
    amount = route.maximum_cost
    if amount > 0 and actual + pending + amount > project.budget_limit:
        raise DomainError("BUDGET_EXCEEDED", "模型调用将超过项目预算", 409)
    if request.episode_id is not None and policy.episode_limit is not None and amount > 0:
        actual, pending = project_costs(db, project.id, episode_id=request.episode_id)
        if actual + pending + amount > policy.episode_limit:
            raise DomainError("BUDGET_EPISODE_EXCEEDED", "模型调用将超过剧集预算", 409)
    if policy.task_limit is not None and amount > 0:
        used = Decimal("0")
        if request.workflow_id is not None:
            for call in db.scalars(
                select(GatewayCall).where(
                    GatewayCall.workflow_id == request.workflow_id,
                    GatewayCall.generation_attempt_id.is_(None),
                )
            ):
                receipt = latest_receipt(db, call.id)
                used += (
                    receipt.actual_cost
                    if receipt and receipt.actual_cost is not None
                    else (call.maximum_cost or Decimal("0"))
                )
        if used + amount > policy.task_limit:
            raise DomainError("BUDGET_TASK_EXCEEDED", "模型调用将超过任务预算", 409)


def reconcile(
    db: Session, call: GatewayCall, actor: str, status: str, usage: GatewayUsage, evidence: str
) -> GatewayReceipt:
    from arclight.application.commands import require_project
    from arclight.domain.generation_requests import safe_metadata

    require_project(db, call.project_id, actor, write=True, owner=True)
    usage = GatewayUsage.model_validate(usage.model_dump(mode="json"))
    if status not in {"SUCCEEDED", "FAILED", "NOT_ACCEPTED"} or not evidence.strip():
        raise DomainError("GATEWAY_EVIDENCE_INVALID", "核账须提供最终状态与明确证据", 422)
    try:
        usage.validate_status(status)
    except ValueError:
        raise DomainError("GATEWAY_EVIDENCE_INVALID", "核账状态与用量不一致", 422) from None
    safe_metadata(evidence)
    if call.generation_attempt_id is not None:
        raise DomainError("GATEWAY_MEDIA_RECONCILIATION", "媒体结果须从已有媒体对账入口核验", 409)
    previous = latest_receipt(db, call.id)
    if previous is not None and previous.kind == "RECONCILIATION":
        if (
            previous.status == status
            and previous.usage_json == usage.model_dump(mode="json")
            and previous.evidence == evidence
        ):
            return previous
        raise DomainError("GATEWAY_RECEIPT_CONFLICT", "已有不同核账证据，不能覆盖", 409)
    if previous is not None and previous.actual_cost is not None:
        raise DomainError("GATEWAY_RECEIPT_CONFLICT", "已有准确费用，不能覆盖", 409)
    if status == "NOT_ACCEPTED" and usage.actual_cost != 0:
        raise DomainError("GATEWAY_EVIDENCE_INVALID", "未接收证据必须确认零费用", 422)
    if usage.actual_cost is None:
        raise DomainError("GATEWAY_EVIDENCE_INVALID", "核账必须提供明确费用", 422)
    row = GatewayReceipt(
        call_id=call.id,
        kind="RECONCILIATION",
        status=status,
        error_code=None,
        usage_json=usage.model_dump(mode="json"),
        actual_cost=usage.actual_cost,
        evidence=evidence,
        actor=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        call.project_id,
        actor,
        "model_gateway.reconciled",
        call.id,
        {"receipt_id": str(row.id), "status": status, "actual_cost": str(usage.actual_cost)},
    )
    return row
