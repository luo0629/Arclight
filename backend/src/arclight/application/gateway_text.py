"""Bind the existing five text operations to durable gateway governance."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import (
    GatewayCapabilities,
    GatewayFailure,
    GatewayRequest,
    GatewayRoute,
    GatewayUsage,
)
from arclight.domain.versions import digest
from arclight.providers.prompts import TEXT_MAX_TOKENS


@dataclass(frozen=True)
class TextRuntime:
    factory: sessionmaker
    settings: Settings
    run_id: UUID
    revision: int


_runtime: ContextVar[TextRuntime | None] = ContextVar("model_gateway_text_runtime", default=None)


@contextmanager
def runtime_scope(factory: sessionmaker, settings: Settings, run_id: UUID, revision: int):
    token = _runtime.set(TextRuntime(factory, settings, run_id, revision))
    try:
        yield
    finally:
        _runtime.reset(token)


def frozen_settings(settings: Settings) -> None:
    runtime = _runtime.get()
    if runtime is not None:
        _runtime.set(TextRuntime(runtime.factory, settings, runtime.run_id, runtime.revision))


def execution_actor(db: Session, run) -> str:
    from arclight.persistence.models import AuditEvent, Episode, Project

    actor = run.input_json.get("created_by") or db.scalar(
        select(AuditEvent.actor_id)
        .where(AuditEvent.resource_id == run.id)
        .order_by(AuditEvent.created_at)
        .limit(1)
    )
    if actor:
        return actor
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    project = db.get(Project, episode.project_id)
    assert project is not None
    return project.owner_id


def validate_publication(db: Session, run_id: UUID) -> None:
    """Recheck mutable scope after external I/O before publishing an artifact."""
    from arclight.application.commands import require_episode
    from arclight.persistence.models import User, WorkflowRun

    run = db.get(WorkflowRun, run_id)
    assert run is not None
    actor = execution_actor(db, run)
    require_episode(db, run.episode_id, actor, write=True)
    if actor != "service-owner":
        user = db.get(User, UUID(actor), populate_existing=True)
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "调用者已不可执行", 403)


def invoke_text[T](provider, stage: str, data: dict, call: Callable[[], T]) -> T:
    runtime = _runtime.get()
    if runtime is None:
        return call()
    from arclight.application.model_gateway import invoke, route_id
    from arclight.persistence.models import Episode, WorkflowRun
    from arclight.providers.text import MockTextProvider

    settings = getattr(provider, "settings", runtime.settings)
    policy = settings.model_gateway_policy
    mock = isinstance(provider, MockTextProvider) or settings.provider == "mock"
    with runtime.factory() as db:
        run = db.get(WorkflowRun, runtime.run_id)
        assert run is not None
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        source: Literal["ACCOUNT_SNAPSHOT", "SERVER"] = (
            "ACCOUNT_SNAPSHOT" if run.input_json.get("_text_service") else "SERVER"
        )
        actor = execution_actor(db, run)
        route = GatewayRoute(
            provider="mock"
            if mock
            else (
                settings.llm_supplier if settings.llm_supplier != "custom" else settings.provider
            ),
            model="mock-text" if mock else settings.llm_model,
            config_version=digest(
                {
                    "provider": settings.provider,
                    "model": settings.llm_model,
                    "base_url": settings.llm_base_url,
                    "supplier": settings.llm_supplier,
                    "response_format": settings.llm_response_format,
                }
            ),
            credential_source="NONE" if mock else source,
            capabilities=GatewayCapabilities(
                version=f"text-{settings.llm_response_format}:1",
                tasks=frozenset({"script", "storyboard", "analyze", "outline", "rewrite"}),
                parameters=frozenset({"max_tokens"}),
            ),
            maximum_cost=Decimal("0") if mock else policy.text_maximum_cost,
            pricing_version="mock-free:1" if mock else policy.pricing_version,
        )
        request = GatewayRequest(
            project_id=episode.project_id,
            episode_id=episode.id,
            workflow_id=run.id,
            actor=actor,
            operation_key=f"text:{run.id}:{runtime.revision}:{stage}",
            task=stage,
            input_sha256=digest(data),
            parameters={"max_tokens": TEXT_MAX_TOKENS},
            authorized_routes=frozenset({route_id(route)}),
            execution=_professional_execution(run, stage, runtime.revision),
        )

    def usage(_result) -> GatewayUsage:
        if mock:
            return GatewayUsage(actual_cost=Decimal("0"))
        return getattr(provider, "last_usage", GatewayUsage())

    # The account's endpoint/key snapshot is resolved before reaching this layer.
    # No fallback route is authorized by the existing UI.
    def safe_call():
        try:
            return call()
        except Exception as exc:
            if mock:
                raise GatewayFailure("PROVIDER_FAILED", not_accepted=True) from None
            if isinstance(exc, DomainError) and exc.code in {
                "INVALID_SERVICE_URL",
                "SERVICE_DNS_UNAVAILABLE",
                "SERVICE_ADDRESS_BLOCKED",
                "SERVICE_REDIRECT_BLOCKED",
            }:
                raise GatewayFailure(
                    exc.code, not_accepted=exc.code != "SERVICE_REDIRECT_BLOCKED"
                ) from None
            raise

    try:
        return invoke(
            runtime.factory,
            request,
            route,
            safe_call,
            policy=policy.model_copy(
                update={
                    "timeout_seconds": min(policy.timeout_seconds, settings.llm_timeout_seconds)
                }
            ),
            usage=usage,
        )
    except DomainError as exc:
        if mock and exc.code == "GATEWAY_NOT_ACCEPTED":
            raise ValueError("Mock text execution failed") from None
        raise


def _professional_execution(run, stage, revision):
    from arclight.domain.agent_limits import AgentExecutionContext
    from arclight.domain.director_creation import DIRECTOR_VERSIONS
    from arclight.domain.screenwriter_creation import SCREENWRITER_VERSION
    from arclight.domain.screenwriter_revision import REVISION_WORKFLOWS

    if run.workflow_version not in {SCREENWRITER_VERSION} | REVISION_WORKFLOWS | DIRECTOR_VERSIONS:
        return None
    return AgentExecutionContext(
        run_id=run.id,
        agent_name="director" if stage == "storyboard" else "screenwriter",
        stage=stage,
        step_key=f"text:{revision}:{stage}",
    )
