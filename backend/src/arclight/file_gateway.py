"""Independent bounded streaming gateway; no database or S3 signing credentials.

Run with uvicorn arclight.file_gateway:create_gateway --factory --no-access-log.
Every request obtains a fresh grant before consuming upload bytes or opening S3.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from tempfile import TemporaryFile
from typing import Literal
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, Request
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from arclight.storage.transfer_protocol import TransferGrant

CHUNK_BYTES = 64 * 1024
TRANSFER_TIMEOUT = 300


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Invalid private endpoint")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCLIGHT_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    file_gateway_secret: SecretStr = SecretStr("local-file-gateway-change-before-deploying")
    file_gateway_api_url: str = "http://api:8000"
    s3_endpoint_url: str = "http://minio:9000"
    max_connections: int = Field(default=64, ge=1, le=512)

    @model_validator(mode="after")
    def validate_endpoints(self) -> "GatewaySettings":
        for url in (self.file_gateway_api_url, self.s3_endpoint_url):
            origin(url)
            parsed = urlsplit(url)
            if parsed.path not in {"", "/"} or parsed.query:
                raise ValueError("Private endpoints cannot include a path or query")
        if self.environment == "production":
            secret = self.file_gateway_secret.get_secret_value()
            if len(secret) < 32 or secret == "local-file-gateway-change-before-deploying":
                raise ValueError("Production requires an independent file gateway secret")
        return self


def rejected(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": "文件传输未获授权或暂不可用"}},
        status_code=status,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


async def verified_download(upstream: httpx.Response, approved: TransferGrant) -> Response:
    """Validate one exact object before sending bytes from the same bounded disk copy."""
    spool = None
    try:
        with anyio.fail_after(TRANSFER_TIMEOUT):
            spool = anyio.wrap_file(await anyio.to_thread.run_sync(TemporaryFile))
            checksum = sha256()
            size = 0
            async for chunk in upstream.aiter_raw(CHUNK_BYTES):
                size += len(chunk)
                if size > approved.byte_size:
                    raise ValueError("Storage response exceeds exact file size")
                checksum.update(chunk)
                await spool.write(chunk)
            if size != approved.byte_size or checksum.hexdigest() != approved.checksum:
                raise ValueError("Storage response differs from the registered file")
            await spool.seek(0)
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        if spool is not None:
            with anyio.CancelScope(shield=True):
                await spool.aclose()
        return rejected(502, "TRANSFER_UNAVAILABLE")
    except BaseException:
        if spool is not None:
            with anyio.CancelScope(shield=True):
                await spool.aclose()
        raise
    finally:
        # Cleanup must preserve the original rejection or cancellation.
        with anyio.CancelScope(shield=True), suppress(httpx.HTTPError, OSError):
            await upstream.aclose()

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                with anyio.fail_after(TRANSFER_TIMEOUT):
                    chunk = await spool.read(CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                await spool.aclose()

    chunks = body()

    class VerifiedDownloadResponse(StreamingResponse):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Sending headers can fail before the body iterator starts.
                with anyio.CancelScope(shield=True):
                    try:
                        await chunks.aclose()
                    finally:
                        if spool is not None:
                            await spool.aclose()

    return VerifiedDownloadResponse(
        chunks,
        headers={
            "Content-Length": str(approved.byte_size),
            "Content-Type": "application/octet-stream",
            "Content-Disposition": upstream.headers.get("content-disposition", "attachment"),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(spool.aclose),
    )


def create_gateway(
    settings: GatewaySettings | None = None,
    *,
    authorization_transport: httpx.AsyncBaseTransport | None = None,
    storage_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    configuration = settings or GatewaySettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        limits = httpx.Limits(max_connections=configuration.max_connections)
        async with (
            httpx.AsyncClient(
                transport=authorization_transport, trust_env=False, timeout=10, limits=limits
            ) as authorization,
            httpx.AsyncClient(
                transport=storage_transport, trust_env=False, timeout=30, limits=limits
            ) as storage,
        ):
            app.state.authorization = authorization
            app.state.storage = storage
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    def health() -> dict:
        return {"status": "ok"}

    async def grant(request: Request, ticket: str, method: str) -> TransferGrant | Response:
        if request.url.query or len(ticket) > 4096:
            return rejected(403, "TRANSFER_REVOKED")
        try:
            response = await request.app.state.authorization.post(
                configuration.file_gateway_api_url.rstrip("/")
                + "/internal/file-transfers/authorize",
                json={"ticket": ticket, "method": method},
                headers={
                    "X-File-Gateway-Key": configuration.file_gateway_secret.get_secret_value()
                },
            )
            if response.status_code != 200:
                return rejected(
                    403 if response.status_code in {401, 403, 404, 409, 422} else 503,
                    "TRANSFER_REVOKED",
                )
            result = TransferGrant.model_validate(response.json())
            if origin(result.url) != origin(configuration.s3_endpoint_url):
                raise ValueError("Unexpected storage origin")
            if result.method != ("PUT" if method == "POST" else "GET"):
                raise ValueError("Unexpected storage action")
            allowed = {"content-type", "x-amz-meta-sha256"} if method == "POST" else set()
            if {name.lower() for name in result.headers} != allowed:
                raise ValueError("Unexpected upstream headers")
            return result
        except (httpx.HTTPError, ValueError, ValidationError):
            return rejected(503, "TRANSFER_UNAVAILABLE")

    @app.post("/file-transfers/{ticket}")
    async def upload(request: Request, ticket: str) -> Response:
        approved = await grant(request, ticket, "POST")
        if isinstance(approved, Response):
            return approved
        if request.headers.get("content-type") != approved.headers["Content-Type"]:
            return rejected(403, "TRANSFER_TYPE_MISMATCH")
        # No multipart parser, temporary disk file, or full-body buffering is needed.
        try:
            if int(request.headers.get("content-length", "-1")) != approved.byte_size:
                return rejected(400, "TRANSFER_SIZE_MISMATCH")
        except ValueError:
            return rejected(400, "TRANSFER_SIZE_MISMATCH")

        async def body() -> AsyncIterator[bytes]:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > approved.byte_size:
                    raise ValueError("Transfer exceeds declared size")
                for offset in range(0, len(chunk), CHUNK_BYTES):
                    yield chunk[offset : offset + CHUNK_BYTES]
            if size != approved.byte_size:
                raise ValueError("Transfer shorter than declared size")

        try:
            with anyio.fail_after(TRANSFER_TIMEOUT):
                async with request.app.state.storage.stream(
                    "PUT",
                    approved.url,
                    content=body(),
                    headers={**approved.headers, "Content-Length": str(approved.byte_size)},
                ) as response:
                    if response.status_code not in {200, 204}:
                        return rejected(502, "TRANSFER_UNAVAILABLE")
            return Response(status_code=204, headers={"Cache-Control": "private, no-store"})
        except (httpx.HTTPError, ValueError, TimeoutError, ClientDisconnect):
            return rejected(400, "TRANSFER_INCOMPLETE")

    @app.get("/file-transfers/{ticket}")
    async def download(request: Request, ticket: str) -> Response:
        approved = await grant(request, ticket, "GET")
        if isinstance(approved, Response):
            return approved
        # Client cookies, authorization, ranges and proxy headers never reach storage.
        try:
            upstream = await request.app.state.storage.send(
                httpx.Request("GET", approved.url), stream=True
            )
        except httpx.HTTPError:
            return rejected(503, "TRANSFER_UNAVAILABLE")
        if (
            upstream.status_code != 200
            or upstream.headers.get("content-length") != str(approved.byte_size)
            or upstream.headers.get("content-encoding", "identity") != "identity"
        ):
            await upstream.aclose()
            return rejected(502, "TRANSFER_UNAVAILABLE")

        if approved.checksum is not None:
            return await verified_download(upstream, approved)

        async def body() -> AsyncIterator[bytes]:
            try:
                with anyio.fail_after(TRANSFER_TIMEOUT):
                    size = 0
                    async for chunk in upstream.aiter_raw(CHUNK_BYTES):
                        size += len(chunk)
                        if size > approved.byte_size:
                            raise ValueError("Storage response exceeds exact file size")
                        yield chunk
                    if size != approved.byte_size:
                        raise ValueError("Incomplete storage response")
            finally:
                # Cancellation/disconnect must release the upstream connection too.
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        return StreamingResponse(
            body(),
            headers={
                "Content-Length": str(approved.byte_size),
                "Content-Type": "application/octet-stream",
                "Content-Disposition": upstream.headers.get("content-disposition", "attachment"),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
