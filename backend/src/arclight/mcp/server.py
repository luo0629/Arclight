"""Private MCP project queries bound to an Agent run."""

import logging
from collections.abc import Collection

from mcp import types
from mcp.server import Server
from pydantic import ValidationError

from arclight.application.agent_project_reads import ProjectReads
from arclight.application.agent_tools import AgentToolContext
from arclight.application.shared_agent_reads import (
    PROJECT_READ_TOOLS,
    ProjectReadResult,
    validate_project_read_scope,
)
from arclight.core.errors import DomainError

logger = logging.getLogger(__name__)


def _tool_error(code: str, status_code: int) -> types.CallToolResult:
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=code)],
        structured_content={"code": code, "status_code": status_code},
    )


def create_project_read_server(
    tool_names: Collection[str], *, context: AgentToolContext | None = None
) -> Server[None]:
    """Bind an already authorized tool list and, for calls, the current transaction."""
    tools = {
        tool.name: tool
        for tool in PROJECT_READ_TOOLS
        if tool.name in tool_names
        and (context is None or (context.agent_name in tool.agents and context.stage in tool.steps))
    }
    output_schema = ProjectReadResult.model_json_schema()
    declarations = [
        types.Tool(
            name=tool.name,
            description=tool.parameters.__doc__,
            input_schema=tool.parameters.model_json_schema(),
            output_schema=output_schema,
            annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False),
        )
        for tool in tools.values()
    ]

    async def list_tools(_context, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=declarations)

    async def call_tool(_context, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = tools.get(params.name)
        if context is None or tool is None:
            return _tool_error("AGENT_NOT_ALLOWED", 403)
        try:
            request = tool.parameters.model_validate(params.arguments or {})
        except ValidationError:
            return _tool_error("INVALID_ARGUMENTS", 422)
        try:
            validate_project_read_scope(context, request)
            result = tool.read(ProjectReads(context.db, context.actor_id), request)
        except DomainError as exc:
            return _tool_error(exc.code, exc.status_code)
        except Exception:
            logger.error("Arclight MCP read failed")
            return _tool_error("EXECUTION_FAILED", 500)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_dump_json())],
            structured_content=result.model_dump(mode="json"),
        )

    return Server("arclight", on_list_tools=list_tools, on_call_tool=call_tool)
