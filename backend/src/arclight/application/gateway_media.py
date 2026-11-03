"""Media uses the same gateway while GEN remains the sole attempt/budget authority."""

from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from arclight.application.model_gateway import invoke, route_id
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import (
    GatewayCapabilities,
    GatewayFailure,
    GatewayPolicy,
    GatewayRequest,
    GatewayRoute,
    GatewayUsage,
)
from arclight.domain.versions import digest
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.providers.media_capabilities import resolve_parameters
from arclight.providers.media_submission import (
    MediaSubmission,
    MediaSubmissionProvider,
    SubmissionReceipt,
)


def submit(
    factory: sessionmaker,
    provider: MediaSubmissionProvider,
    request: MediaSubmission,
    timeout: float,
    policy: GatewayPolicy,
) -> SubmissionReceipt:
    from sqlalchemy import select

    with factory() as db:
        job = db.get(GenerationJob, request.job_id)
        assert job is not None
        quote = db.scalar(
            select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id)
        )
        spec = request.snapshot["context"]["generation"]
        route = GatewayRoute(
            provider=job.provider,
            model=job.model,
            config_version=spec["model_config_version"],
            credential_source="NONE",
            capabilities=GatewayCapabilities(
                version=provider.capabilities.version,
                tasks=frozenset(provider.capabilities.task_types),
                parameters=resolve_parameters(job.provider, job.model),
                media=provider.capabilities,
            ),
            maximum_cost=quote.maximum_cost
            if quote
            else Decimal("0")
            if job.provider == "mock"
            else None,
            pricing_version=quote.pricing_version if quote else "mock-free:1",
        )
        call = GatewayRequest(
            project_id=job.project_id,
            episode_id=job.episode_id,
            generation_attempt_id=request.attempt_id,
            actor=job.created_by,
            operation_key=f"media:{request.attempt_id}",
            task=job.task_type,
            input_sha256=digest(request.snapshot),
            parameters=spec["parameters"],
            authorized_routes=frozenset({route_id(route)}),
        )

    def submit_adapter() -> SubmissionReceipt:
        receipt = SubmissionReceipt.model_validate(provider.submit(request).model_dump(mode="json"))
        if receipt.result is not None and len(receipt.result.output_handles) != spec.get(
            "batch_size", 1
        ):
            raise GatewayFailure("INVALID_RESPONSE")
        if (
            receipt.status in {"SUBMITTED", "RUNNING"}
            and provider.capabilities.execution_mode == "SYNC"
        ):
            raise GatewayFailure("INVALID_RESPONSE")
        return receipt

    try:
        return invoke(
            factory,
            call,
            route,
            submit_adapter,
            policy=policy.model_copy(
                update={
                    "timeout_seconds": max(0.001, min(timeout, policy.timeout_seconds)),
                    "max_attempts": 1,
                }
            ),
            usage=lambda value: GatewayUsage(
                actual_cost=value.cost,
                images=len(value.result.output_handles)
                if value.result and call.task == "IMAGE"
                else None,
            ),
            outcome=lambda value: value.status,
            crash_errors=(RuntimeError,),
        )
    except DomainError as exc:
        if exc.code == "GATEWAY_RECONCILIATION_REQUIRED":
            return SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
        return SubmissionReceipt(
            status="FAILED",
            error_code="BUDGET_EXCEEDED"
            if exc.code.startswith("BUDGET_")
            else "PROVIDER_UNAVAILABLE",
            cost=Decimal("0"),
        )
