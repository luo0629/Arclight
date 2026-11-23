"""Authenticated Streamable HTTP access to Arclight project reads."""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp import types
from mcp.server import Server
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import Receive, Scope, Send

from arclight.application.agent_project_reads import ProjectReads
from arclight.application.shared_agent_reads import PROJECT_READ_TOOLS, ProjectReadResult
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.core.security import decode_token, session_user

logger = logging.getLogger(__name__)
MCP_PATH = "/api/mcp"


class McpEndpoint:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await scope["app"].state.mcp_app(scope, receive, send)


@dataclass(frozen=True, slots=True)
class ArclightTokenVerifier:
    factory: sessionmaker[Session]
    settings: Settings

    def _verify(self, token: str) -> AccessToken | None:
        try:
            claims = decode_token(token, self.settings, "access")
            with self.factory() as db:
                actor = str(session_user(db, claims).id)
        except DomainError:
            return None
        return AccessToken(token=token, client_id=actor, subject=actor, scopes=[])

    async def verify_token(self, token: str) -> AccessToken | None:
        return await asyncio.to_thread(self._verify, token)


def _tool_error(code: str) -> types.CallToolResult:
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=code)],
    )


def create_mcp_app(factory: sessionmaker[Session], settings: Settings) -> Starlette:
    tools = {tool.name: tool for tool in PROJECT_READ_TOOLS}
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

    def read(params: types.CallToolRequestParams, actor: str) -> ProjectReadResult:
        tool = tools.get(params.name)
        if tool is None:
            raise DomainError("UNKNOWN_TOOL", "工具不存在", 404)
        try:
            request = tool.parameters.model_validate(params.arguments or {})
        except ValidationError:
            raise DomainError("INVALID_ARGUMENTS", "工具参数无效", 422) from None
        with factory() as db, db.no_autoflush:
            return tool.read(ProjectReads(db, actor), request)

    async def call_tool(_context, params: types.CallToolRequestParams) -> types.CallToolResult:
        token = get_access_token()
        if token is None or token.subject is None:
            return _tool_error("UNAUTHORIZED")
        try:
            result = await asyncio.to_thread(read, params, token.subject)
        except DomainError as exc:
            return _tool_error(exc.code)
        except Exception:
            logger.error("Arclight MCP read failed")
            return _tool_error("READ_FAILED")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_dump_json())],
            structured_content=result.model_dump(mode="json"),
        )

    verifier = ArclightTokenVerifier(factory, settings)
    server = Server("arclight", on_list_tools=list_tools, on_call_tool=call_tool)
    origins = [origin.rstrip("/") for origin in settings.auth_allowed_origins]
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        token_verifier=verifier,
        stateless_http=True,
        json_response=True,
        max_request_body_size=settings.max_request_bytes,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[
                "api",
                "api:8000",
                "localhost",
                "localhost:*",
                "127.0.0.1",
                "127.0.0.1:*",
                "[::1]",
                "[::1]:*",
                *(urlsplit(origin).netloc for origin in origins),
            ],
            allowed_origins=[
                "http://localhost:*",
                "http://127.0.0.1:*",
                "http://[::1]:*",
                *origins,
            ],
        ),
    )
    # Reuse the application's JWT verifier without advertising an OAuth issuer.
    app.add_middleware(AuthContextMiddleware)
    app.add_middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier))
    return app
