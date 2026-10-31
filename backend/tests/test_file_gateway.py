import json
import threading

import anyio
import httpx
import pytest
from conftest import serve_asgi
from fastapi.testclient import TestClient
from pydantic import ValidationError

from arclight.file_gateway import GatewaySettings, create_gateway


def gateway(authorization, storage):
    class StreamingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # MockTransport eagerly reads requests before dispatch, hiding streaming behavior.
            return await storage(request)

    return create_gateway(
        GatewaySettings(environment="test", s3_endpoint_url="http://private-storage:9000"),
        authorization_transport=httpx.MockTransport(authorization),
        storage_transport=StreamingTransport(),
    )


def approved(method="GET", size=3, **values):
    return httpx.Response(
        200,
        json={
            "method": method,
            "url": "http://private-storage:9000/private/key?signature=private",
            "headers": {}
            if method == "GET"
            else {"Content-Type": "image/png", "x-amz-meta-sha256": "a" * 64},
            "byte_size": size,
            **values,
        },
    )


def test_denial_precedes_upload_body_and_storage_and_is_never_cached():
    reads, decisions = [], []

    async def authorization(request):
        decisions.append(json.loads(request.content))
        assert "authorization" not in request.headers
        return httpx.Response(403)

    async def storage(request):
        pytest.fail("Denied transfer must not contact storage")

    async def exercise():
        app = gateway(authorization, storage)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://gateway"
            ) as client,
        ):

            async def body():
                reads.append(True)
                yield b"user bytes"

            for _ in range(2):
                response = await client.post("/file-transfers/old-ticket", content=body())
                assert response.status_code == 403
                assert response.headers["cache-control"] == "private, no-store"

    anyio.run(exercise)
    assert not reads
    assert decisions == [{"ticket": "old-ticket", "method": "POST"}] * 2


@pytest.mark.parametrize(
    "grant",
    [
        {"url": "http://other-host/private"},
        {"url": "http://user:password@private-storage:9000/private"},
        {"method": "PUT"},
        {"headers": {"Authorization": "sensitive"}},
    ],
)
def test_gateway_cannot_become_an_open_proxy(grant):
    async def authorization(request):
        return approved(**grant)

    async def storage(request):
        pytest.fail("Untrusted upstream must not be contacted")

    with TestClient(gateway(authorization, storage)) as client:
        result = client.get("/file-transfers/ticket")
        assert result.status_code == 503
        assert "password" not in result.text and "other-host" not in result.text


def test_upload_stream_is_bounded_and_client_headers_do_not_reach_storage():
    chunks = []

    async def authorization(request):
        return approved(method="PUT", size=200_000)

    async def storage(request):
        assert request.method == "PUT"
        assert request.headers["content-type"] == "image/png"
        assert request.headers["x-amz-meta-sha256"] == "a" * 64
        for name in ("cookie", "authorization", "x-forwarded-for", "x-extra"):
            assert name not in request.headers
        async for part in request.stream:
            chunks.append(part)
            assert len(part) <= 64 * 1024
        return httpx.Response(200)

    with TestClient(gateway(authorization, storage)) as client:
        assert (
            client.post(
                "/file-transfers/ticket",
                content=b"x" * 200_000,
                headers={
                    "Content-Type": "image/png",
                    "Authorization": "private",
                    "Cookie": "secret=1",
                    "X-Forwarded-For": "forged",
                    "X-Extra": "untrusted",
                },
            ).status_code
            == 204
        )
    assert b"".join(chunks) == b"x" * 200_000


@pytest.mark.parametrize("case", ["short", "long", "no-length", "type", "oversize", "disconnect"])
def test_upload_limits_and_partial_streams_fail_closed(case):
    contacted = []

    async def authorization(request):
        return approved(method="PUT", size=33 * 1024 * 1024 if case == "oversize" else 3)

    async def storage(request):
        contacted.append(True)
        async for _ in request.stream:
            pass
        return httpx.Response(200)

    async def exercise():
        app = gateway(authorization, storage)
        async with app.router.lifespan_context(app):
            events = [
                {
                    "type": "http.request",
                    "body": b"xx" if case == "short" else b"xxxx",
                    "more_body": False,
                }
            ]
            if case == "disconnect":
                events = [{"type": "http.disconnect"}]
            headers = [(b"content-type", b"text/html" if case == "type" else b"image/png")]
            if case != "no-length":
                headers.append((b"content-length", b"3"))
            messages = []

            async def receive():
                return events.pop(0)

            async def send(message):
                messages.append(message)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "method": "POST",
                    "scheme": "http",
                    "path": "/file-transfers/ticket",
                    "query_string": b"",
                    "headers": headers,
                    "server": ("gateway", 80),
                    "client": ("client", 1),
                },
                receive,
                send,
            )
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            assert status in {400, 403, 413}

    anyio.run(exercise)
    assert bool(contacted) == (case in {"short", "long", "disconnect"})


@pytest.mark.parametrize("status", [302, 403, 500])
def test_storage_errors_and_redirects_do_not_leak_private_response(status):
    calls = []

    async def authorization(request):
        return approved()

    async def storage(request):
        calls.append(str(request.url))
        return httpx.Response(
            status, headers={"location": "http://untrusted-host"}, content=b"secret bucket/key"
        )

    with TestClient(gateway(authorization, storage)) as client:
        response = client.get("/file-transfers/ticket")
        assert response.status_code == 502
        assert "bucket" not in response.text
        assert "location" not in response.headers
    assert len(calls) == 1


def test_download_disconnect_closes_storage_stream():
    closed = threading.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            await anyio.sleep(20)
            yield b"x" * 65536

        async def aclose(self):
            closed.set()

    async def authorization(request):
        return approved(size=131072)

    async def storage(request):
        assert "authorization" not in request.headers
        assert "range" not in request.headers
        return httpx.Response(200, headers={"content-length": "131072"}, stream=SlowStream())

    with serve_asgi(gateway(authorization, storage)) as address:
        with httpx.stream(
            "GET",
            address + "/file-transfers/ticket",
            trust_env=False,
            headers={"Range": "bytes=0-3"},
        ) as response:
            assert response.status_code == 200
            assert next(response.iter_bytes(65536)) == b"x" * 65536
        assert closed.wait(5), "Disconnect must release the upstream connection"


def test_gateway_requires_private_endpoint_and_production_secret():
    for values in (
        {"environment": "production"},
        {"file_gateway_api_url": "http://api/path?key=private"},
        {"s3_endpoint_url": "file:///tmp"},
    ):
        with pytest.raises(ValidationError):
            GatewaySettings(**values)
