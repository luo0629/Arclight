"""Prepare one authorized Agent turn and account for it through the shared gateway."""

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_tools import AgentToolRegistry, available_agent_tools
from arclight.application.context_evidence import read_trace
from arclight.application.context_images import load_context_images
from arclight.application.gateway_text import execution_actor
from arclight.application.model_gateway import invoke, route_id
from arclight.application.model_services import run_settings
from arclight.application.production_versions import text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext
from arclight.domain.agent_model import (
    AgentModelRequest,
    AgentModelResponse,
    AgentToolDeclaration,
)
from arclight.domain.model_gateway import GatewayCapabilities, GatewayRequest, GatewayRoute
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.agent_model import OpenAICompatibleAgentProvider, check_capabilities


def invoke_agent[T: BaseModel](
    factory: sessionmaker[Session],
    settings: Settings,
    request: AgentModelRequest,
    registry: AgentToolRegistry,
    result_model: type[T],
    *,
    execution: AgentExecutionContext | None = None,
) -> AgentModelResponse[T]:
    """Return tool selections or a validated final result without executing either."""
    request = AgentModelRequest.model_validate(request.model_dump(mode="json"))
    if execution is not None and (
        execution.run_id != request.run_id
        or execution.agent_name != request.agent_name
        or execution.stage != request.stage
        or execution.kind != "MODEL"
    ):
        raise DomainError("AGENT_SCOPE_INVALID", "Agent 通信与执行器行动标识不匹配", 409)
    with factory() as db:
        run = db.get(WorkflowRun, request.run_id)
        if run is None:
            raise DomainError("NOT_FOUND", "工作流不存在", 404)
        episode = db.get(Episode, run.episode_id)
        if episode is None:
            raise DomainError("NOT_FOUND", "工作流作用范围不存在", 404)
        actor = execution_actor(db, run)
        definitions = available_agent_tools(
            db, registry, run.id, request.agent_name, actor, request.stage
        )
        tools = tuple(
            AgentToolDeclaration(
                name=tool.name,
                description=(tool.parameters_model.__doc__ or tool.name)[:1000],
                parameters=tool.parameters_model.model_json_schema(),
            )
            for tool in definitions
        )
        settings = run_settings(
            text_settings(db, episode.project_id, run, settings),
            run.input_json.get("_text_service"),
            db,
        )
        if settings.provider != "openai_compatible":
            raise DomainError("GATEWAY_CAPABILITY_REJECTED", "此 Provider 尚未实现 Agent 协议", 422)
        capabilities = check_capabilities(settings, request, tools)
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
            credential_source="ACCOUNT_SNAPSHOT"
            if run.input_json.get("_text_service")
            else "SERVER",
            capabilities=GatewayCapabilities(
                version=request.protocol_version,
                tasks=frozenset({request.stage}),
                parameters=frozenset(
                    {
                        "max_tokens",
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
                    "result_schema": result_model.model_json_schema(),
                    "trace": trace,
                }
            ),
            parameters={
                "max_tokens": request.max_tokens,
                "input_images": len(request.image_ids()),
                "agent_version": request.agent_version,
                "prompt_version": request.prompt_version,
                "result_schema_version": request.result_schema_version,
                "result_schema_sha256": digest(result_model.model_json_schema()),
            },
            authorized_routes=frozenset({route_id(route)}),
            execution=execution,
        )
    # The preparation transaction and business row locks are released before external I/O.
    provider = OpenAICompatibleAgentProvider(settings)
    return invoke(
        factory,
        call,
        route,
        lambda: provider.complete(request, tools, result_model, images),
        policy=policy.model_copy(
            update={"timeout_seconds": min(policy.timeout_seconds, settings.llm_timeout_seconds)}
        ),
        usage=lambda _: provider.last_usage,
    )
