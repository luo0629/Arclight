import asyncio
import json

import pytest

from arclight.api.protection import RequestProtection


@pytest.mark.parametrize(
    "headers,chunks,status",
    [
        ([(b"content-length", b"16")], [b"{}"], 413),
        ([(b"content-length", b"-1")], [b"{}"], 400),
        ([(b"content-length", b"bad")], [b"{}"], 400),
        ([], [b"12345", b"678901"], 413),
        ([], [b"12345", b"67890"], 200),
    ],
)
def test_body_guard_counts_chunks_and_validates_length(headers, chunks, status):
    messages = []
    received = []

    async def downstream(scope, receive, send):
        received.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def exercise():
        events = [
            {"type": "http.request", "body": chunk, "more_body": n < len(chunks) - 1}
            for n, chunk in enumerate(chunks)
        ]

        async def receive():
            return events.pop(0) if events else {"type": "http.disconnect"}

        async def send(event):
            messages.append(event)

        await RequestProtection(downstream, 10, 30)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/projects",
                "headers": headers,
                "state": {"request_id": "test-body"},
            },
            receive,
            send,
        )

    asyncio.run(exercise())
    assert messages[0]["status"] == status
    if status == 200:
        assert received == [b"".join(chunks)]
    else:
        assert not received
        assert json.loads(messages[-1]["body"])["error"]["request_id"] == "test-body"


def test_body_guard_disconnect_does_not_call_application():
    called = []

    async def downstream(*args):
        called.append(True)

    async def receive():
        return {"type": "http.disconnect"}

    asyncio.run(
        RequestProtection(downstream, 10, 30)(
            {"type": "http", "method": "POST", "path": "/", "headers": []},
            receive,
            None,
        )
    )
    assert not called


def test_auth_rate_limit_and_request_id(settings, migrated, monkeypatch):
    from fastapi.testclient import TestClient

    from arclight.main import create_app

    settings.auth_requests_per_minute = 2
    now = [100.0]
    monkeypatch.setattr("arclight.api.protection.time.monotonic", lambda: now[0])
    with TestClient(create_app(settings)) as client:
        for _ in range(2):
            assert client.post("/api/v1/auth/login", json={}).status_code == 422
        response = client.post("/api/v1/auth/refresh", headers={"X-Request-ID": "rate-test"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.headers["X-Request-ID"] == "rate-test"
        assert response.json()["error"]["request_id"] == "rate-test"
        assert client.get("/health/live").status_code == 200
        now[0] += 61
        assert client.post("/api/v1/auth/refresh").status_code == 401


def test_http_request_size_guard(settings, migrated):
    from fastapi.testclient import TestClient

    from arclight.main import create_app

    settings.max_request_bytes = 1024
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/auth/login", content=b"x" * 1025)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
