"""Prepare one authorized Agent turn and account for it through the shared gateway."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_tools import AgentToolRegistry, available_agent_tools
from arclight.application.context_evidence import read_trace
from arclight.application.context_images import load_context_images
from arclight.application.gateway_text import execution_actor
from arclight.application.model_gateway import invoke, lock_gate, route_id
from arclight.application.model_services import (
    recheck_text_credential,
    run_settings,
    text_credential_identity,
)
from arclight.application.production_versions import text_settings
from arclight.application.shared_agent_reads import agent_tool_declarations
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext
from arclight.domain.agent_model import (
    AgentModelRequest,
    AgentModelResponse,
)
from arclight.domain.model_gateway import (
    GatewayCapabilities,
    GatewayFailure,
    GatewayRequest,
    GatewayRoute,
    GatewayUsage,
)
from arclight.domain.versions import digest
from arclight.domain.video_temporal_reviewer import TEMPORAL_AGENT
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.agent_model import agent_transport, check_capabilities
from arclight.providers.llm import dispatch_inference


@dataclass(frozen=True, slots=True)
class AgentOutputProjection[T: BaseModel]:
    """Map a typed provider decision onto the unchanged durable result contract."""

    schema_version: str
    wire_model: type[BaseModel]
    materialize: Callable[[AgentModelRequest, BaseModel], T]


def invoke_agent[T: BaseModel](
    factory: sessionmaker[Session],
    settings: Settings,
    request: AgentModelRequest,
    registry: AgentToolRegistry,
    result_model: type[T],
    *,
    execution: AgentExecutionContext | None = None,
    output_projection: AgentOutputProjection[T] | None = None,
    on_admit: Callable[[Session, UUID], None] | None = None,
    on_result: Callable[[Session, UUID, AgentModelResponse[T]], None] | None = None,
) -> AgentModelResponse[T]:
    """Return tool selections or a validated final result without executing either."""
    request = AgentModelRequest.model_validate(request.model_dump(mode="json"))
    wire_model = result_model if output_projection is None else output_projection.wire_model
    if (
        output_projection is not None
        and request.result_schema_version != output_projection.schema_version
    ):
        # An already frozen request may not dispatch under a different wire contract.
        # Committed model actions still replay their unchanged canonical result.
        raise DomainError("AGENT_LOOP_CONFLICT", "恢复不能更换冻结的模型输出合同", 409)
    wire_schema = wire_model.model_json_schema()
    response_model = cast(
        type[AgentModelResponse[T]], AgentModelResponse.__class_getitem__(result_model)
    )
    if execution is not None and (
        execution.run_id != request.run_id
        or execution.agent_name != request.agent_name
        or execution.stage != request.stage
        or execution.kind != "MODEL"
    ):
        raise DomainError("AGENT_SCOPE_INVALID", "Agent 通信与执行器行动标识不匹配", 409)
    with factory() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, request.run_id)
        if run is None:
            raise DomainError("NOT_FOUND", "工作流不存在", 404)
        episode = db.get(Episode, run.episode_id)
        if episode is None:
            raise DomainError("NOT_FOUND", "工作流作用范围不存在", 404)
        actor = execution_actor(db, run)
        from arclight.domain.narrative_review import NARRATIVE_REVIEW_VERSION

        if run.workflow_version == NARRATIVE_REVIEW_VERSION:
            from arclight.application.narrative_review import current_work

            current_work(db, episode.project_id, run.id)
        definitions = available_agent_tools(
            db, registry, run.id, request.agent_name, actor, request.stage
        )
        tools = agent_tool_declarations(definitions)
        snapshot = run.input_json.get("_text_service")
        settings = run_settings(text_settings(db, episode.project_id, run, settings), snapshot, db)
        transport = agent_transport(settings)
        if transport is None:
            raise DomainError("GATEWAY_CAPABILITY_REJECTED", "此 Provider 尚未实现 Agent 协议", 422)
        capabilities = check_capabilities(settings, request, tools)
        if request.agent_version == TEMPORAL_AGENT:
            from arclight.application.video_review_evidence import temporal_gateway_evidence

            trace, images = temporal_gateway_evidence(request)
        else:
            trace = read_trace(db, run, request.stage, actor, request.evidence_round)
            images = load_context_images(
                db,
                settings,
                run,
                request.stage,
                actor,
                request.image_ids(),
                through_round=request.evidence_round,
                capabilities=capabilities,
            )
        policy = settings.model_gateway_policy
        route = GatewayRoute(
            provider=settings.llm_supplier
            if settings.llm_supplier != "custom"
            else settings.provider,
            model=settings.llm_model,
            config_version=digest(
                {
                    "base_url": settings.llm_base_url,
                    "supplier": settings.llm_supplier,
                    "model": settings.llm_model,
                    "response_format": settings.llm_response_format,
                    "agent": capabilities.model_dump(mode="json"),
                }
            ),
            credential_source="ACCOUNT_SNAPSHOT" if snapshot else "SERVER",
            capabilities=GatewayCapabilities(
                version=request.protocol_version,
                tasks=frozenset({request.stage}),
                parameters=frozenset(
                    {
                        "input_images",
                        "agent_version",
                        "prompt_version",
                        "result_schema_version",
                        "result_schema_sha256",
                    }
                ),
                agent=capabilities,
            ),
            maximum_cost=policy.text_maximum_cost,
            pricing_version=policy.pricing_version,
        )
        call = GatewayRequest(
            project_id=episode.project_id,
            episode_id=episode.id,
            workflow_id=run.id,
            actor=actor,
            operation_key=f"agent:{run.id}:{request.operation_key}",
            task=request.stage,
            input_sha256=digest(
                {
                    "request": request.model_dump(mode="json"),
                    "tools": [tool.model_dump(mode="json") for tool in tools],
                    "result_schema": wire_schema,
                    "trace": trace,
                }
            ),
            parameters={
                "input_images": len(request.image_ids()),
                "agent_version": request.agent_version,
                "prompt_version": request.prompt_version,
                "result_schema_version": request.result_schema_version,
                "result_schema_sha256": digest(wire_schema),
            },
            authorized_routes=frozenset({route_id(route)}),
            execution=execution,
        )
    # The preparation transaction and business row locks are released before external I/O.
    provider = transport(settings)
    identity = text_credential_identity(actor, snapshot)

    def check_binding(db: Session, call_id: UUID) -> None:
        recheck_text_credential(db, request.run_id, snapshot, settings)
        if run.workflow_version == NARRATIVE_REVIEW_VERSION:
            current_work(db, episode.project_id, request.run_id)
        if on_admit is not None:
            on_admit(db, call_id)

    def publish_round(
        db: Session, call_id: UUID, result: tuple[AgentModelResponse[T], GatewayUsage]
    ) -> None:
        if on_result is not None:
            on_result(db, call_id, result[0])

    def complete() -> tuple[AgentModelResponse[T], GatewayUsage]:
        if output_projection is None:
            return dispatch_inference(
                settings, identity, lambda: provider.complete(request, tools, result_model, images)
            )
        response, usage = dispatch_inference(
            settings, identity, lambda: provider.complete(request, tools, wire_model, images)
        )
        try:
            final = (
                output_projection.materialize(request, response.final)
                if response.final is not None
                else None
            )
            return response_model.model_validate(
                response.model_dump(mode="json", exclude={"final"})
                | {"final": final.model_dump(mode="json") if final is not None else None}
            ), usage
        except (ValueError, KeyError, TypeError) as exc:
            # It is a completed, potentially billable response, not safe to resend.
            raise GatewayFailure("INVALID_RESPONSE", completed=True, usage=usage) from exc

    result, _usage = invoke(
        factory,
        call,
        route,
        complete,
        policy=policy.model_copy(
            update={"timeout_seconds": min(policy.timeout_seconds, settings.llm_timeout_seconds)}
        ),
        usage=lambda result: result[1],
        on_admit=check_binding,
        on_result=publish_round if on_result is not None else None,
    )
    return result
