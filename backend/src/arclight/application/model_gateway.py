"""One durable admission point for text and media, with conservative failure semantics."""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.gateway_accounting import admit_budget, latest_receipt
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import (
    GatewayFailure,
    GatewayPolicy,
    GatewayRequest,
    GatewayRoute,
    GatewayUsage,
)
from arclight.domain.versions import digest
from arclight.persistence.base import utcnow
from arclight.persistence.model_gateway import (
    GatewayCall,
    GatewayCircuit,
    GatewayGate,
    GatewayReceipt,
)
from arclight.providers.media_io import bounded_call


def route_id(route: GatewayRoute) -> str:
    return digest(route.model_dump(mode="json"))


def lock_gate(db: Session) -> None:
    # UPDATE obtains the actual SQLite write lock as well as a PostgreSQL row lock.
    changed = db.scalar(
        update(GatewayGate)
        .where(GatewayGate.id == "global")
        .values(id="global")
        .returning(GatewayGate.id)
    )
    if changed != "global":
        raise DomainError("GATEWAY_STORAGE_UNAVAILABLE", "模型网关写入门禁不可用", 503)


def utc(value):
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def admit(
    db: Session, request: GatewayRequest, route: GatewayRoute, policy: GatewayPolicy
) -> GatewayCall:
    from arclight.application.commands import require_project
    from arclight.persistence.models import Episode, User, WorkflowRun

    lock_gate(db)
    require_project(db, request.project_id, request.actor, write=True)
    if request.actor != "service-owner":
        user = db.get(User, UUID(request.actor), populate_existing=True)
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "调用者已不可执行", 403)
    if request.episode_id is not None:
        episode = db.get(Episode, request.episode_id, populate_existing=True)
        if (
            episode is None
            or episode.project_id != request.project_id
            or episode.status == "ARCHIVED"
        ):
            raise DomainError("GATEWAY_SCOPE_INVALID", "调用剧集不属于可执行范围", 409)
    if request.workflow_id is not None:
        run = db.get(WorkflowRun, request.workflow_id)
        if run is None or run.episode_id != request.episode_id:
            raise DomainError("GATEWAY_SCOPE_INVALID", "调用任务不属于此剧集", 409)
    request = GatewayRequest.model_validate(request.model_dump(mode="json"))
    route = GatewayRoute.model_validate(route.model_dump(mode="json"))
    if (
        request.task not in route.capabilities.tasks
        or set(request.parameters) - route.capabilities.parameters
    ):
        raise DomainError("GATEWAY_CAPABILITY_REJECTED", "模型不支持请求的任务或参数", 422)
    if route_id(route) not in request.authorized_routes:
        raise DomainError("GATEWAY_ROUTE_FORBIDDEN", "模型路由未获本次调用授权", 403)
    existing = list(
        db.scalars(
            select(GatewayCall)
            .where(
                GatewayCall.project_id == request.project_id,
                GatewayCall.operation_key == request.operation_key,
            )
            .order_by(GatewayCall.sequence)
        )
    )
    for call in existing:
        if (
            call.input_sha256 != request.input_sha256
            or call.task != request.task
            or call.parameters_json != request.parameters
            or call.actor != request.actor
            or call.episode_id != request.episode_id
            or call.workflow_id != request.workflow_id
            or call.generation_attempt_id != request.generation_attempt_id
        ):
            raise DomainError("GATEWAY_IDEMPOTENCY_CONFLICT", "同一模型调用不能更换输入", 409)
        receipt = latest_receipt(db, call.id)
        if receipt is None or receipt.status != "NOT_ACCEPTED":
            raise DomainError(
                "GATEWAY_RECONCILIATION_REQUIRED", "已有模型调用，须核验原结果后才能继续", 409
            )
    if request.workflow_id is not None:
        for call in db.scalars(
            select(GatewayCall).where(
                GatewayCall.workflow_id == request.workflow_id, GatewayCall.task == request.task
            )
        ):
            receipt = latest_receipt(db, call.id)
            if receipt is None or receipt.status in {"RESULT_UNKNOWN", "SUBMITTED", "RUNNING"}:
                raise DomainError(
                    "GATEWAY_RECONCILIATION_REQUIRED", "该任务有未知模型结果，禁止重复收费调用", 409
                )
    if len(existing) >= policy.max_attempts:
        raise DomainError("GATEWAY_ATTEMPT_LIMIT", "模型调用已达到尝试上限", 409)
    identity = digest({"project": str(request.project_id), "route": route_id(route)})
    circuit = db.get(GatewayCircuit, identity, populate_existing=True)
    if circuit is None:
        circuit = GatewayCircuit(
            id=identity, project_id=request.project_id, failures=0, paused=False, revision=0
        )
        db.add(circuit)
        db.flush()
    now = utcnow()
    if circuit.paused:
        raise DomainError("GATEWAY_PAUSED", "此项目模型路由已暂停", 409)
    if circuit.opened_until is not None and utc(circuit.opened_until) > now:
        raise DomainError("GATEWAY_CIRCUIT_OPEN", "模型服务熔断中，请稍后再试", 409)
    if circuit.probe_until is not None and utc(circuit.probe_until) > now:
        raise DomainError("GATEWAY_CIRCUIT_OPEN", "模型服务正在恢复探测，请稍后再试", 409)
    recent = (
        select(func.count())
        .select_from(GatewayCall)
        .where(GatewayCall.created_at > now - timedelta(seconds=60))
    )
    if (db.scalar(recent) or 0) >= policy.global_per_minute or (
        db.scalar(recent.where(GatewayCall.actor == request.actor)) or 0
    ) >= policy.user_per_minute:
        raise DomainError("GATEWAY_RATE_LIMITED", "模型调用频率达到限制", 429)
    admit_budget(db, request, route)
    row = GatewayCall(
        project_id=request.project_id,
        episode_id=request.episode_id,
        workflow_id=request.workflow_id,
        generation_attempt_id=request.generation_attempt_id,
        actor=request.actor,
        operation_key=request.operation_key,
        sequence=len(existing) + 1,
        task=request.task,
        input_sha256=request.input_sha256,
        circuit_id=identity,
        route_json=route.model_dump(mode="json"),
        parameters_json=request.parameters,
        maximum_cost=route.maximum_cost,
    )
    db.add(row)
    db.flush()
    if circuit.opened_until is not None:
        circuit.probe_call_id = row.id
        circuit.probe_until = now + timedelta(seconds=policy.timeout_seconds)
    return row


def finish(
    db: Session,
    call_id: UUID,
    status: str,
    usage: GatewayUsage,
    policy: GatewayPolicy,
    error: str | None = None,
) -> None:
    lock_gate(db)
    call = db.get(GatewayCall, call_id)
    assert call is not None
    if latest_receipt(db, call_id) is not None:
        raise DomainError("GATEWAY_RECEIPT_CONFLICT", "模型调用已有回执", 409)
    usage = GatewayUsage.model_validate(usage.model_dump(mode="json"))
    usage.validate_status(status)
    db.add(
        GatewayReceipt(
            call_id=call.id,
            kind="INITIAL",
            status=status,
            error_code=error,
            usage_json=usage.model_dump(mode="json"),
            actual_cost=usage.actual_cost,
            evidence=None,
            actor="model-gateway",
        )
    )
    circuit = db.get(GatewayCircuit, call.circuit_id)
    assert circuit is not None
    was_probe = circuit.probe_call_id == call.id
    if was_probe:
        circuit.probe_until = None
        circuit.probe_call_id = None
    if status == "SUCCEEDED" and (circuit.opened_until is None or was_probe):
        circuit.failures = 0
        circuit.opened_until = None
    elif status in {"FAILED", "RESULT_UNKNOWN"} or error in {
        "PROVIDER_FAILED",
        "PROVIDER_UNAVAILABLE",
        "RESULT_UNKNOWN",
        "INVALID_RESPONSE",
    }:
        circuit.failures += 1
        if circuit.failures >= policy.failure_threshold:
            circuit.opened_until = utcnow() + timedelta(seconds=policy.cooldown_seconds)
    circuit.revision += 1
    db.flush()


def invoke[T](
    factory: sessionmaker[Session],
    request: GatewayRequest,
    route: GatewayRoute,
    call: Callable[[], T],
    *,
    policy: GatewayPolicy,
    usage: Callable[[T], GatewayUsage] = lambda _: GatewayUsage(),
    outcome: Callable[[T], str] = lambda _: "SUCCEEDED",
    crash_errors: tuple[type[Exception], ...] = (),
    fallback: tuple[GatewayRoute, Callable[[], T]] | None = None,
) -> T:
    started = utcnow()
    current_route, current_call = route, call
    for attempt in range(policy.max_attempts):
        with factory.begin() as db:
            row = admit(db, request, current_route, policy)
            call_id = row.id
        try:
            remaining = policy.timeout_seconds - (utcnow() - started).total_seconds()
            result = bounded_call(current_call, remaining)
            actual = GatewayUsage.model_validate(usage(result).model_dump(mode="json"))
            status = outcome(result)
            actual.validate_status(status)
        except Exception as exc:
            safe_failure = isinstance(exc, GatewayFailure) and exc.not_accepted
            error = (
                exc.code
                if isinstance(exc, GatewayFailure)
                and exc.code
                in {"PROVIDER_FAILED", "PROVIDER_UNAVAILABLE", "INPUT_INVALID", "INVALID_RESPONSE"}
                else "RESULT_UNKNOWN"
            )
            with factory.begin() as db:
                finish(
                    db,
                    call_id,
                    "NOT_ACCEPTED" if safe_failure else "RESULT_UNKNOWN",
                    GatewayUsage(actual_cost=Decimal("0")) if safe_failure else GatewayUsage(),
                    policy,
                    error,
                )
            if isinstance(exc, crash_errors):
                raise RuntimeError(
                    "Adapter execution crashed; call retained for reconciliation"
                ) from None
            if (
                safe_failure
                and isinstance(exc, GatewayFailure)
                and exc.transient
                and attempt + 1 < policy.max_attempts
            ):
                if fallback is not None:
                    candidate, candidate_call = fallback
                    if (
                        request.task not in candidate.capabilities.tasks
                        or set(request.parameters) - candidate.capabilities.parameters
                    ):
                        raise DomainError(
                            "GATEWAY_CAPABILITY_REJECTED", "备用模型能力不兼容", 422
                        ) from None
                    current_route, current_call = candidate, candidate_call
                continue
            code = "GATEWAY_NOT_ACCEPTED" if safe_failure else "GATEWAY_RECONCILIATION_REQUIRED"
            raise DomainError(code, "模型调用失败，已保存安全诊断", 502) from None
        with factory.begin() as db:
            finish(db, call_id, status, actual, policy)
        return result
    raise DomainError("GATEWAY_ATTEMPT_LIMIT", "模型调用已达到尝试上限", 409)
