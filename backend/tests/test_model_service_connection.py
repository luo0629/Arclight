import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from test_model_services import account

from arclight.persistence.models import ServiceCredential
from arclight.providers import network, service_connection

PATH = "/api/v1/me/model-services"
BASE = "https://independent.example/proxy/v1"
KEY = "synthetic-connection-key"
MODEL = "provider/example-model"


def draft(**changes):
    return {
        "base_url": BASE + "/",
        "model": MODEL,
        "api_key": KEY,
        "expected_revision": 0,
        **changes,
    }


@pytest.fixture
def upstream(monkeypatch):
    stub = SimpleNamespace(
        requests=[],
        status=200,
        body={"data": [{"id": MODEL}]},
        failure=None,
        resolve_public=network.resolve_public,
    )

    def send(request):
        stub.requests.append(request)
        if stub.failure:
            raise stub.failure
        return httpx.Response(
            stub.status,
            headers={"Location": "https://redirect.example/"},
            stream=httpx.ByteStream(json.dumps(stub.body).encode()),
        )

    class OfflineTransport(network.PublicHTTPTransport):
        def __init__(self, dns_timeout):
            super().__init__(dns_timeout)
            self.inner.close()
            self.inner = httpx.MockTransport(send)

    monkeypatch.setattr(service_connection, "PublicHTTPTransport", OfflineTransport)
    monkeypatch.setattr(network, "resolve_public", lambda *_: "8.8.8.8")
    return stub


def test_draft_connection_is_read_only_and_uses_the_configured_base(client, upstream):
    account(client)
    response = client.post(PATH + "/text/connection", json=draft())
    assert response.status_code == 200
    assert response.json()["status"] == "model_found"
    assert response.headers["Cache-Control"] == "no-store"
    assert KEY not in response.text
    (request,) = upstream.requests
    assert request.method == "GET"
    assert str(request.url) == "https://8.8.8.8/proxy/v1/models"
    assert request.headers["Host"] == "independent.example"
    assert request.extensions["sni_hostname"] == "independent.example"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.read() == b""
    assert client.get(PATH).json()["services"] == []
    assert client.get(PATH + "/audit").json()["items"] == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ServiceCredential)) == 0


@pytest.mark.parametrize(
    ("kind", "config", "path", "method", "header", "payload", "result"),
    [
        (
            "image",
            {"supplier": "google", "base_url": "", "model": "models/gemini-image"},
            "/v1beta/models/gemini-image",
            "GET",
            "x-goog-api-key",
            {"name": "models/gemini-image"},
            "model_found",
        ),
        (
            "video",
            {"protocol": "seedance"},
            "/proxy/v1",
            "HEAD",
            "Authorization",
            {"data": [{"id": MODEL}]},
            "reachable",
        ),
        (
            "speech",
            {"supplier": "siliconflow", "base_url": ""},
            "/v1/models",
            "GET",
            "Authorization",
            {"data": [{"id": MODEL}]},
            "model_found",
        ),
    ],
)
def test_read_only_check_follows_protocol(
    client, upstream, kind, config, path, method, header, payload, result
):
    account(client)
    upstream.body = payload
    response = client.post(f"{PATH}/{kind}/connection", json=draft(**config))
    assert response.status_code == 200 and response.json()["status"] == result
    (request,) = upstream.requests
    assert request.url.path == path and request.method == method
    assert request.headers[header] == (KEY if header == "x-goog-api-key" else f"Bearer {KEY}")
    assert request.read() == b""


@pytest.mark.parametrize(
    ("status", "body", "result", "message"),
    [
        (200, {"data": [{"id": "other-model"}]}, "reachable", "模型查询未能确认"),
        (200, "<html>login</html>", "reachable", "模型查询未能确认"),
        (200, {"padding": "x" * (512 * 1024)}, "reachable", "模型查询未能确认"),
        (401, {"error": KEY}, "failed", "请检查 API Key"),
        (403, {"error": KEY}, "failed", "请检查 API Key"),
        (404, {}, "reachable", "模型查询未能确认"),
        (405, {}, "reachable", "模型查询未能确认"),
        (429, {}, "failed", "限制了请求频率"),
        (502, {"error": KEY}, "failed", "HTTP 502"),
        (307, {}, "reachable", "重定向"),
    ],
    ids=[
        "missing",
        "non-catalog",
        "large",
        "key",
        "access",
        "404",
        "405",
        "429",
        "502",
        "redirect",
    ],
)
def test_probe_reports_only_safe_diagnostics(client, upstream, status, body, result, message):
    account(client)
    upstream.status = status
    upstream.body = body
    response = client.post(PATH + "/text/connection", json=draft())
    assert response.status_code == 200
    assert response.json()["status"] == result
    assert message in response.json()["message"]
    assert KEY not in response.text
    assert len(upstream.requests) == 1


def test_saved_key_stays_on_its_account_destination_and_protocol(client, settings, upstream):
    settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    account(client)
    path = PATH + "/text"
    saved = client.put(path, json=draft(mode="custom"))
    assert saved.status_code == 200
    payload = draft(api_key=None, expected_revision=1)
    response = client.post(path + "/connection", json=payload)
    assert response.json()["status"] == "model_found"
    assert upstream.requests[0].headers["Authorization"] == f"Bearer {KEY}"
    for changed in [
        {"base_url": "https://other.example/v1"},
        {"protocol": "qwen_chat"},
    ]:
        denied = client.post(path + "/connection", json={**payload, **changed})
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "SERVICE_KEY_REENTRY_REQUIRED"
    assert client.post(path + "/connection", json=draft(api_key=None)).status_code == 409
    assert client.get(PATH).json()["services"][0]["revision"] == 1
    account(client)
    denied = client.post(path + "/connection", json=draft(api_key=None))
    assert denied.json()["error"]["code"] == "SERVICE_INCOMPLETE"
    assert len(upstream.requests) == 1


def test_probe_rejects_nonpersonal_invalid_and_blocked_requests(client, upstream, monkeypatch):
    path = PATH + "/text/connection"
    assert client.post(path, json=draft()).status_code == 403
    account(client)
    for patch, code in [
        ({"base_url": "http://localhost/v1"}, "INVALID_SERVICE_URL"),
        ({"supplier": "openai"}, "SUPPLIER_MISMATCH"),
        ({"protocol": "seedance"}, "INVALID_PROTOCOL"),
        ({"api_key": KEY + "\ninvalid"}, "INVALID_SERVICE_KEY"),
    ]:
        response = client.post(path, json=draft(**patch))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
        assert KEY not in response.text
    with monkeypatch.context() as guarded:
        guarded.setattr(network, "resolve_public", upstream.resolve_public)
        getaddrinfo = network.socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            if host == "independent.example":
                return [
                    (network.socket.AF_INET, network.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
                ]
            return getaddrinfo(host, *args, **kwargs)

        guarded.setattr(network.socket, "getaddrinfo", resolve)
        response = client.post(path, json=draft())
    assert response.json()["error"]["code"] == "SERVICE_ADDRESS_BLOCKED"
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (httpx.ReadTimeout(KEY), 503, "SERVICE_PROBE_TIMEOUT"),
        (httpx.ConnectError(KEY), 502, "SERVICE_PROBE_UNAVAILABLE"),
    ],
)
def test_probe_transport_errors_are_sanitized(client, upstream, failure, status, code):
    account(client)
    upstream.failure = failure
    response = client.post(PATH + "/text/connection", json=draft())
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert KEY not in response.text
