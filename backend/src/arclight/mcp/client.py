"""Use an MCP session over memory streams within the Agent's transaction."""

import asyncio
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager

import anyio
from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams

from arclight.application.agent_tools import AgentToolContext
from arclight.core.errors import DomainError
from arclight.mcp.server import create_project_read_server


@asynccontextmanager
async def project_read_session(server: Server[None]) -> AsyncIterator[ClientSession]:
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
        try:
            async with ClientSession(*client_streams, read_timeout_seconds=10) as session:
                await session.initialize()
                yield session
        finally:
            tasks.cancel_scope.cancel()


async def _list_project_reads(names: Collection[str]) -> dict[str, types.Tool]:
    async with project_read_session(create_project_read_server(names)) as session:
        result = await session.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    if result.next_cursor or len(tools) != len(result.tools) or tools.keys() != set(names):
        raise DomainError("MCP_TOOL_CATALOG_INVALID", "MCP 工具清单与本次授权不一致", 502)
    return tools


def list_project_reads(names: Collection[str]) -> dict[str, types.Tool]:
    return asyncio.run(_list_project_reads(names))


async def _call_project_read(
    context: AgentToolContext, name: str, arguments: dict
) -> types.CallToolResult:
    server = create_project_read_server((name,), context=context)
    async with project_read_session(server) as session:
        return await session.call_tool(name, arguments)


def call_project_read(context: AgentToolContext, name: str, arguments: dict) -> dict:
    result = asyncio.run(_call_project_read(context, name, arguments))
    if result.is_error:
        details = result.structured_content or {}
        code, status = details.get("code"), details.get("status_code")
        if not isinstance(code, str) or type(status) is not int or not 400 <= status <= 599:
            raise DomainError("MCP_QUERY_FAILED", "MCP 查询失败", 502)
        raise DomainError(code, "MCP 查询被拒绝", status)
    if result.structured_content is None:
        raise DomainError("MCP_TOOL_RESULT_INVALID", "MCP 查询缺少结构化结果", 502)
    return result.structured_content
