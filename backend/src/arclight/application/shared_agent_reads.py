"""Run grants and contracts for internal Agent MCP tools."""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import Field, JsonValue
from sqlalchemy.orm import Session

from arclight.application.agent_tools import (
    AgentToolContext,
    AgentToolDefinition,
    AgentToolRegistry,
    grant_agent_tools,
)
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentToolDeclaration
from arclight.domain.agent_state import AgentStepId
from arclight.domain.agent_tools import (
    MAX_AGENT_TOOL_RESULT_BYTES,
    AgentName,
    AgentToolContract,
    AgentToolGrantRequest,
)
from arclight.domain.context_trace import MAX_EVIDENCE_ROUNDS
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.script_narrative import FrozenWritingQuery
from arclight.persistence.agent_limits import AgentRunLimit

if TYPE_CHECKING:
    from arclight.application.agent_project_reads import ProjectReads


class EpisodeRead(AgentToolContract):
    episode_id: UUID


class ProjectRead(AgentToolContract):
    project_id: UUID


class RunRead(AgentToolContract):
    run_id: UUID


class ReadContent(EpisodeRead):
    """Read an approved script by exact version. Use scene_no to select one scene."""

    version_id: UUID
    scene_no: Annotated[int | None, Field(strict=True, ge=1)] = None


class QueryStoryFacts(EpisodeRead):
    """Query facts and character knowledge at a story time in a frozen run."""

    query: FrozenWritingQuery


class ReadShotContext(EpisodeRead):
    """Read one shot in an exact storyboard and its authorized content references."""

    storyboard_id: UUID
    shot_id: UUID


class ReadVisualReferences(ProjectRead):
    """Read the visual specification and approved references at an exact version."""

    version_id: UUID


class ReadProductionStatus(RunRead):
    """Read a Producer run's plan, dependency states and budget summary."""


class ReadReviewEvidence(RunRead):
    """Read recorded evidence from an authorized run and stage."""

    stage: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[a-z_]+$")]
    through_round: Annotated[int | None, Field(ge=0, le=MAX_EVIDENCE_ROUNDS)] = None


class PreviewChangeImpact(ProjectRead):
    """Preview downstream impact from changing an exact asset version."""

    version_id: UUID


class ProjectReadResult(AgentToolContract):
    data: dict[str, JsonValue]


def _in_run_scope(context: AgentToolContext, run_id: UUID, *, delegated_review: bool) -> bool:
    if run_id == context.run_id:
        return True
    if not delegated_review or context.agent_name != "producer":
        return False
    control = context.db.get(AgentRunLimit, run_id)
    return (
        control is not None
        and control.parent_run_id == context.run_id
        and control.root_run_id == context.run_id
        and control.actor_id == context.actor_id
    )


def validate_project_read_scope(context: AgentToolContext, request: AgentToolContract) -> None:
    if isinstance(request, EpisodeRead):
        if request.episode_id != context.episode_id:
            raise DomainError("NOT_FOUND", "对象不在本次运行的剧集范围", 404)
        if isinstance(request, QueryStoryFacts) and request.query.run_id != context.run_id:
            raise DomainError("NOT_FOUND", "叙事上下文不在本次运行的范围", 404)
    elif isinstance(request, ProjectRead):
        if request.project_id != context.project_id:
            raise DomainError("NOT_FOUND", "对象不在本次运行的项目范围", 404)
    elif isinstance(request, RunRead):
        if not _in_run_scope(
            context, request.run_id, delegated_review=isinstance(request, ReadReviewEvidence)
        ):
            raise DomainError("NOT_FOUND", "对象不在本次运行的范围", 404)
    else:
        raise DomainError("INVALID_ARGUMENTS", "项目查询未定义访问范围", 422)


@dataclass(frozen=True, slots=True)
class ProjectReadTool:
    name: str
    parameters: type[AgentToolContract]
    agents: frozenset[AgentName]
    steps: frozenset[AgentStepId]

    def read(self, service: "ProjectReads", request: AgentToolContract) -> ProjectReadResult:
        data = getattr(service, self.name)(**dict(request))
        result = {"data": data}
        try:
            safe_metadata(result)
            result_size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        except (TypeError, ValueError) as exc:
            raise DomainError("CONTEXT_INPUT_UNSAFE", "查询结果包含不支持的数据", 422) from exc
        if result_size > MAX_AGENT_TOOL_RESULT_BYTES:
            raise DomainError("CONTEXT_TOO_LARGE", "查询结果超限，请缩小查询范围", 422)
        return ProjectReadResult.model_validate(result)

    def definition(self) -> AgentToolDefinition:
        def read(context: AgentToolContext, request: AgentToolContract) -> ProjectReadResult:
            from arclight.mcp.client import call_project_read

            return ProjectReadResult.model_validate(
                call_project_read(context, self.name, request.model_dump(mode="json"))
            )

        return AgentToolDefinition(
            name=self.name,
            allowed_agents=self.agents,
            allowed_steps=self.steps,
            parameters_model=self.parameters,
            result_model=ProjectReadResult,
            scope_validator=lambda context, request: None,
            handler=read,
        )


PROJECT_READ_TOOLS = (
    ProjectReadTool(
        "arclight_read_content",
        ReadContent,
        frozenset({"screenwriter", "director", "art_director", "continuity_reviewer"}),
        frozenset({"outline", "script", "rewrite", "storyboard", "storyboard_review"}),
    ),
    ProjectReadTool(
        "arclight_query_story_facts",
        QueryStoryFacts,
        frozenset({"screenwriter", "director", "continuity_reviewer"}),
        frozenset({"outline", "script", "rewrite", "storyboard", "storyboard_review"}),
    ),
    ProjectReadTool(
        "arclight_read_shot_context",
        ReadShotContext,
        frozenset({"director", "art_director", "continuity_reviewer"}),
        frozenset({"storyboard", "storyboard_review"}),
    ),
    ProjectReadTool(
        "arclight_read_visual_references",
        ReadVisualReferences,
        frozenset({"director", "art_director", "continuity_reviewer"}),
        frozenset({"storyboard", "storyboard_review"}),
    ),
    ProjectReadTool(
        "arclight_read_production_status",
        ReadProductionStatus,
        frozenset({"producer"}),
        frozenset({"producer"}),
    ),
    ProjectReadTool(
        "arclight_read_review_evidence",
        ReadReviewEvidence,
        frozenset({"producer", "continuity_reviewer"}),
        frozenset({"producer", "storyboard", "storyboard_review"}),
    ),
    ProjectReadTool(
        "arclight_preview_change_impact",
        PreviewChangeImpact,
        frozenset({"producer", "screenwriter", "director"}),
        frozenset({"producer", "script", "rewrite", "storyboard"}),
    ),
)

_MCP_DEFINITIONS = tuple(tool.definition() for tool in PROJECT_READ_TOOLS)


def with_project_reads(registry: AgentToolRegistry) -> AgentToolRegistry:
    # Persistent execution can rebuild the runtime using an already populated registry.
    return registry.with_tools(
        tool for tool in _MCP_DEFINITIONS if registry.get(tool.name) is not tool
    )


def agent_tool_declarations(
    definitions: tuple[AgentToolDefinition, ...],
) -> tuple[AgentToolDeclaration, ...]:
    from arclight.mcp.client import list_project_reads

    allowed = {definition.name for definition in definitions}
    names = {tool.name for tool in PROJECT_READ_TOOLS} & allowed
    mcp_tools = list_project_reads(names) if names else {}
    declarations = []
    for definition in definitions:
        if definition.name in mcp_tools:
            tool = mcp_tools[definition.name]
            declaration = AgentToolDeclaration(
                name=tool.name,
                description=(tool.description or tool.name)[:1000],
                parameters=dict(tool.input_schema),
            )
        else:
            declaration = AgentToolDeclaration(
                name=definition.name,
                description=(definition.parameters_model.__doc__ or definition.name)[:1000],
                parameters=definition.parameters_model.model_json_schema(),
            )
        declarations.append(declaration)
    return tuple(declarations)


def grant_professional_agent_tools(
    db: Session,
    request: AgentToolGrantRequest,
    actor: str,
    registry: AgentToolRegistry,
) -> None:
    """Extend the server-owned grant at the normal creation entry points."""
    request = AgentToolGrantRequest(
        run_id=request.run_id,
        agent_name=request.agent_name,
        allowed_tools=[
            *request.allowed_tools,
            *(tool.name for tool in PROJECT_READ_TOOLS if request.agent_name in tool.agents),
        ],
    )
    grant_agent_tools(db, request, actor, with_project_reads(registry))
