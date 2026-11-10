import hashlib
import json
import threading
from io import BytesIO

import anyio
import httpx
import pytest
from conftest import serve_asgi
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from arclight import file_gateway
from arclight.file_gateway import GatewaySettings, create_gateway
from arclight.storage.transfer_protocol import TransferGrant


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


@pytest.fixture
def download_spools(monkeypatch):
    opened = []
    temporary_file = file_gateway.TemporaryFile

    def tracked_file():
        stream = temporary_file()
        opened.append(stream)
        return stream

    monkeypatch.setattr(file_gateway, "TemporaryFile", tracked_file)
    return opened


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


@pytest.mark.parametrize("failure", ["replacement", "truncated", "overlong", "read_error"])
def test_verified_download_rejects_damaged_body_before_releasing_any_bytes(
    failure, download_spools
):
    expected = b"private render bytes\n" * 10000
    expected_sha256 = hashlib.sha256(expected).hexdigest()
    closed = []
    if failure == "replacement":
        actual = expected[:-1] + b"!"
    elif failure == "truncated":
        actual = expected[:-1]
    elif failure == "overlong":
        actual = expected + b"!"
    else:
        actual = expected

    class DamagedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield actual[:65536]
            if failure == "read_error":
                raise httpx.ReadError("private bucket/key?X-Amz-Signature=secret")
            for offset in range(65536, len(actual), 65536):
                yield actual[offset : offset + 65536]

        async def aclose(self):
            closed.append(True)

    async def authorization(request):
        return approved(size=len(expected), checksum=expected_sha256)

    async def storage(request):
        return httpx.Response(
            200,
            headers={
                "content-length": str(len(expected)),
                "x-amz-meta-sha256": expected_sha256,
            },
            stream=DamagedStream(),
        )

    with TestClient(gateway(authorization, storage)) as client:
        response = client.get("/file-transfers/ticket")
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert b"private render bytes" not in response.content
        assert "bucket" not in response.text and "X-Amz" not in response.text
    assert closed, "Failed integrity verification must release the upstream connection"
    assert download_spools and all(stream.closed for stream in download_spools)


def test_verified_download_returns_exact_multichunk_body_and_releases_upstream(download_spools):
    expected = bytes(range(256)) * 16384
    closed = []

    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(expected), 65536):
                yield expected[offset : offset + 65536]

        async def aclose(self):
            closed.append(True)

    async def authorization(request):
        return approved(size=len(expected), checksum=hashlib.sha256(expected).hexdigest())

    async def storage(request):
        assert "authorization" not in request.headers and "range" not in request.headers
        return httpx.Response(
            200,
            headers={
                "content-length": str(len(expected)),
                "content-disposition": "attachment; filename=render.mp4",
            },
            stream=ChunkedStream(),
        )

    with TestClient(gateway(authorization, storage)) as client:
        response = client.get(
            "/file-transfers/ticket",
            headers={"Authorization": "Bearer private", "Range": "bytes=0-3"},
        )
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert response.headers["content-length"] == str(len(expected))
        assert response.headers["content-disposition"] == "attachment; filename=render.mp4"
        assert response.headers["cache-control"] == "private, no-store"
    assert closed, "Verified downloads must release the upstream connection"
    assert download_spools and all(stream.closed for stream in download_spools)


def test_verified_download_disk_write_failure_releases_handles_without_leaking(monkeypatch):
    closed = []
    expected = b"private rendered movie"

    class UnwritableSpool(BytesIO):
        def write(self, data):
            raise OSError("private spool directory is full")

    class StorageStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield expected

        async def aclose(self):
            closed.append(True)

    spool = UnwritableSpool()
    monkeypatch.setattr(file_gateway, "TemporaryFile", lambda: spool)

    async def authorization(request):
        return approved(size=len(expected), checksum=hashlib.sha256(expected).hexdigest())

    async def storage(request):
        return httpx.Response(
            200, headers={"content-length": str(len(expected))}, stream=StorageStream()
        )

    with TestClient(gateway(authorization, storage)) as client:
        response = client.get("/file-transfers/ticket")
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
        assert expected not in response.content and "spool directory" not in response.text
    assert closed and spool.closed


def test_verified_download_cancellation_closes_upstream_and_partial_spool(download_spools):
    closed = []

    async def scenario():
        blocked = anyio.Event()
        expected = b"x" * 131072

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield expected[:65536]
                blocked.set()
                await anyio.sleep_forever()

            async def aclose(self):
                closed.append(True)

        async def download():
            grant = TransferGrant(
                method="GET",
                url="http://private-storage:9000/private/key",
                byte_size=len(expected),
                checksum=hashlib.sha256(expected).hexdigest(),
            )
            await file_gateway.verified_download(
                httpx.Response(200, stream=BlockingStream()), grant
            )

        async with anyio.create_task_group() as group:
            group.start_soon(download)
            with anyio.fail_after(5):
                await blocked.wait()
            group.cancel_scope.cancel()
        assert closed, "Cancelled verification must close its upstream stream"
        assert download_spools and all(stream.closed for stream in download_spools)

    anyio.run(scenario)


@pytest.mark.parametrize("failure", ["read_error", "overlong", "disk_write", "eof_close"])
@pytest.mark.parametrize("close_error", [httpx.CloseError, OSError], ids=["http", "os"])
def test_verified_download_upstream_close_failure_preserves_safe_rejection(
    failure, close_error, download_spools, monkeypatch
):
    expected = b"private render bytes\n" * 10000
    closed = []

    class UnwritableSpool(BytesIO):
        def write(self, data):
            raise OSError("private spool directory is full")

    if failure == "disk_write":
        spool = UnwritableSpool()
        download_spools.append(spool)
        monkeypatch.setattr(file_gateway, "TemporaryFile", lambda: spool)

    class FailingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield expected[:65536]
            if failure == "read_error":
                raise httpx.ReadError("private bucket/key?X-Amz-Signature=secret")
            yield expected[65536:]
            if failure == "overlong":
                yield b"!"

        async def aclose(self):
            closed.append(True)
            raise close_error("private bucket/key?X-Amz-Signature=secret")

    async def authorization(request):
        return approved(size=len(expected), checksum=hashlib.sha256(expected).hexdigest())

    async def storage(request):
        return httpx.Response(
            200, headers={"content-length": str(len(expected))}, stream=FailingCloseStream()
        )

    with TestClient(gateway(authorization, storage)) as client:
        response = client.get("/file-transfers/ticket")
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert b"private render bytes" not in response.content
        assert "bucket" not in response.text and "X-Amz" not in response.text
        assert "spool directory" not in response.text
    assert closed == [True], "Upstream cleanup must be attempted once"
    assert download_spools and all(stream.closed for stream in download_spools)


@pytest.mark.parametrize("close_error", [httpx.CloseError, OSError], ids=["http", "os"])
def test_verified_download_upstream_close_failure_preserves_cancellation(
    close_error, download_spools
):
    closed = []
    cancelled = []

    async def scenario():
        blocked = anyio.Event()
        expected = b"x" * 131072

        class BlockingCloseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield expected[:65536]
                blocked.set()
                await anyio.sleep_forever()

            async def aclose(self):
                closed.append(True)
                raise close_error("private bucket/key?X-Amz-Signature=secret")

        async def download():
            grant = TransferGrant(
                method="GET",
                url="http://private-storage:9000/private/key",
                byte_size=len(expected),
                checksum=hashlib.sha256(expected).hexdigest(),
            )
            try:
                await file_gateway.verified_download(
                    httpx.Response(200, stream=BlockingCloseStream()), grant
                )
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

        async with anyio.create_task_group() as group:
            group.start_soon(download)
            with anyio.fail_after(5):
                await blocked.wait()
            group.cancel_scope.cancel()
        assert cancelled == [True], "Upstream close errors must preserve cancellation"
        assert closed == [True], "Cancelled verification must attempt upstream cleanup once"
        assert download_spools and all(stream.closed for stream in download_spools)

    anyio.run(scenario)


@pytest.mark.parametrize("message_type", ["http.response.start", "http.response.body"])
@pytest.mark.parametrize("failure", ["disconnect", "cancellation"])
def test_verified_download_send_failure_closes_upstream_spool_and_iterator(
    download_spools, message_type, failure
):
    async def scenario():
        expected = bytes(range(256)) * 1024
        upstream_closed = []
        attempted = []
        delivered = bytearray()

        class TrackedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for offset in range(0, len(expected), 65536):
                    yield expected[offset : offset + 65536]

            async def aclose(self):
                upstream_closed.append(True)

        upstream = httpx.Response(200, stream=TrackedStream())
        grant = TransferGrant(
            method="GET",
            url="http://private-storage:9000/private/key",
            byte_size=len(expected),
            checksum=hashlib.sha256(expected).hexdigest(),
        )
        response = await file_gateway.verified_download(upstream, grant)
        iterator = response.body_iterator
        assert upstream_closed == [True]
        assert len(download_spools) == 1 and not download_spools[0].closed

        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            attempted.append(message["type"])
            if message["type"] == message_type:
                if failure == "cancellation":
                    cancel_scope.cancel()
                    await anyio.lowlevel.checkpoint()
                raise OSError("Client disconnected while sending a verified download")
            if message["type"] == "http.response.body":
                delivered.extend(message.get("body", b""))

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
        with anyio.fail_after(5):
            if failure == "disconnect":
                with pytest.raises(ClientDisconnect):
                    await response(scope, receive, send)
            else:
                with anyio.CancelScope() as cancel_scope:
                    await response(scope, receive, send)
                assert cancel_scope.cancelled_caught

        assert attempted == (
            ["http.response.start"]
            if message_type == "http.response.start"
            else ["http.response.start", "http.response.body"]
        )
        assert delivered == b"", "A failed first send must not release body bytes"
        assert upstream_closed == [True]
        assert download_spools[0].closed
        assert iterator.ag_frame is None, "The response body iterator must close immediately"

    anyio.run(scenario)


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
