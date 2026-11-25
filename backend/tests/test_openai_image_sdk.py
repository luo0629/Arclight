"""Verify the single Images SDK path for the account Images protocol."""

import base64
import json
from io import BytesIO

import httpx
import pytest
from openai.resources.images import Images
from PIL import Image
from test_native_providers import CANARY, native_request

from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.service_models import ServiceModelPolicy
from arclight.providers import network
from arclight.providers.media_io import JSON_RESPONSE_LIMIT
from arclight.providers.native_media import NativeMediaProvider


class Chunks(httpx.SyncByteStream):
    def __init__(self, raw: bytes):
        self.raw = raw
        self.closed = False
        self.read_bytes = 0

    def __iter__(self):
        for offset in range(0, len(self.raw), 256 * 1024):
            chunk = self.raw[offset : offset + 256 * 1024]
            self.read_bytes += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def sdk_calls(monkeypatch):
    calls = []
    original = Images.generate

    def observe(self, *args, **kwargs):
        calls.append(self._client.max_retries)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Images, "generate", observe)
    return calls


def route(settings, tmp_path, *, supplier="custom"):
    connection, request = native_request(settings, tmp_path, "openai_image")
    base = "https://api.openai.com/v1" if supplier == "openai" else "https://native.example/api/v1"
    connection.configuration.update(kind="image", supplier=supplier, base_url=base)
    settings.service_model_policies = [
        ServiceModelPolicy(
            kind="image",
            supplier=supplier,
            base_url=base,
            model=connection.configuration["model"],
            protocol="openai_image",
        )
    ]
    return connection, request


@pytest.mark.parametrize("supplier,encoding", [("openai", "base64"), ("custom", "url")])
def test_sdk_keeps_exact_account_request_and_private_result(
    settings, tmp_path, monkeypatch, sdk_calls, supplier, encoding
):
    connection, request = route(settings, tmp_path, supplier=supplier)
    base = connection.configuration["base_url"]
    generation = request.snapshot["context"]["generation"]
    format = "png" if encoding == "base64" else "jpeg"
    generation["output_spec"]["format"] = format
    generation["parameters"] = {"quality": "high"}
    output = BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(output, format=format.upper(), compress_level=0)
    data = output.getvalue()
    signed_url = "https://cdn.example/artifact?signature=fixture"
    source = (
        {"b64_json": base64.b64encode(data).decode()}
        if encoding == "base64"
        else {"url": signed_url}
    )
    raw = json.dumps({"data": [source], "usage": {"input_tokens": 19, "output_tokens": 7}}).encode()
    if encoding == "base64":
        assert len(raw) > JSON_RESPONSE_LIMIT
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-not-the-frozen-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "ambient-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "ambient-project")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer ambient\nHost: ambient.example\nCookie: ambient\nX-Api-Key: ambient",
    )
    requests, streams = [], []

    def handler(req):
        requests.append(req)
        if req.method == "GET":
            assert str(req.url) == signed_url
            assert "authorization" not in req.headers and "x-api-key" not in req.headers
            return httpx.Response(200, content=data)
        stream = Chunks(raw)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    receipt = provider.submit(request)
    assert receipt.status == "SUCCEEDED" and receipt.result is not None
    assert receipt.result.usage == {"images": 1, "input_tokens": 19, "output_tokens": 7}
    assert provider.download(request, receipt.result.output_handles[0], len(data)).data == data
    assert CANARY not in receipt.model_dump_json() and signed_url not in receipt.model_dump_json()
    assert sdk_calls == [0]
    assert len(streams) == 1 and streams[0].closed and streams[0].read_bytes == len(raw)
    assert [req.method for req in requests] == (["POST", "GET"] if encoding == "url" else ["POST"])
    req = requests[0]
    assert str(req.url) == base + "/images/generations"
    assert req.headers["host"] == httpx.URL(base).host
    assert req.headers["authorization"] == "Bearer " + CANARY
    assert req.headers["accept-encoding"] == "identity"
    assert not {"openai-organization", "openai-project", "cookie", "x-api-key"}.intersection(
        req.headers
    )
    assert json.loads(req.content) == {
        "model": "native-model",
        "prompt": "synthetic prompt",
        "size": "1024x1024",
        "quality": "high",
        "n": 1,
        "output_format": format,
    }


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        {"kind": "speech"},
        {"supplier": "openai"},
        {"model": "unverified-model"},
        {"base_url": "https://another.example/api/v1"},
        {"protocol": "cogview"},
    ],
)
def test_sdk_transport_is_defined_by_protocol_not_quote_policy(
    settings, tmp_path, sdk_calls, mismatch
):
    connection, request = route(settings, tmp_path)
    settings.service_model_policies = (
        [settings.service_model_policies[0].model_copy(update=mismatch)] if mismatch else []
    )
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"error": {"code": "insufficient_balance"}})

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayFailure) as caught:
        provider.submit(request)
    assert caught.value.code == "PROVIDER_INSUFFICIENT_BALANCE" and caught.value.not_accepted
    assert len(calls) == 1 and sdk_calls == [0]


@pytest.mark.parametrize(
    "status,payload,code,rejected,completed",
    [
        (200, {"error": {"code": "insufficient_quota"}}, "PROVIDER_QUOTA_EXHAUSTED", True, False),
        (200, {"code": "bad_parameter"}, "INPUT_INVALID", True, False),
        (401, {"error": {"code": "bad_key"}}, "PROVIDER_AUTHENTICATION_FAILED", True, False),
        (429, {"error": {"code": "slow_down"}}, "PROVIDER_RATE_LIMITED", True, False),
        (
            429,
            {"error": {"code": "slow_down"}, "usage": {"prompt_tokens": 19}},
            "PROVIDER_RATE_LIMITED",
            False,
            True,
        ),
        (503, {"error": {"code": "unavailable"}}, "PROVIDER_UNAVAILABLE", False, False),
    ],
)
def test_sdk_retains_native_error_and_usage_receipts(
    settings, tmp_path, sdk_calls, status, payload, code, rejected, completed
):
    connection, request = route(settings, tmp_path)
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(status, json=payload, headers={"Retry-After": "0"})

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayFailure) as caught:
        provider.submit(request)
    failure = caught.value
    assert (failure.code, failure.not_accepted, failure.completed) == (code, rejected, completed)
    assert failure.usage.input_tokens == (19 if completed else None)
    assert len(sent) == 1 and sdk_calls == [0]


def test_sdk_closes_and_limits_non_success_response_body(settings, tmp_path, sdk_calls):
    connection, request = route(settings, tmp_path)
    stream = Chunks(b"x" * (JSON_RESPONSE_LIMIT + 512 * 1024))
    provider = NativeMediaProvider(
        settings,
        connection,
        transport=httpx.MockTransport(lambda _: httpx.Response(503, stream=stream)),
    )
    with pytest.raises(GatewayFailure) as caught:
        provider.submit(request)
    assert caught.value.code == "INVALID_RESPONSE" and not caught.value.not_accepted
    assert stream.closed and JSON_RESPONSE_LIMIT < stream.read_bytes < len(stream.raw)
    assert sdk_calls == [0]


@pytest.mark.parametrize("failure", [None, "timeout", "redirect"])
def test_sdk_uses_public_transport_and_sends_once(
    settings, tmp_path, monkeypatch, sdk_calls, failure
):
    connection, request = route(settings, tmp_path)
    resolved, sent = [], []

    def resolver(host, timeout):
        resolved.append((host, timeout))
        return "8.8.8.8"

    def handler(req):
        sent.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic read timeout", request=req)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://redirect.example/images"})
        return httpx.Response(401)

    def inner_transport(*, trust_env, retries):
        assert not trust_env and retries == 0
        return httpx.MockTransport(handler)

    monkeypatch.setattr(network, "resolve_public", resolver)
    monkeypatch.setattr(network.httpx, "HTTPTransport", inner_transport)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:9999")
    provider = NativeMediaProvider(settings, connection)
    with pytest.raises(GatewayFailure) as caught:
        provider.submit(request)
    assert caught.value.code == (
        "INVALID_RESPONSE" if failure else "PROVIDER_AUTHENTICATION_FAILED"
    )
    assert caught.value.not_accepted is (failure is None)
    assert len(sent) == 1 and sdk_calls == [0]
    assert resolved == [("native.example", settings.llm_dns_timeout_seconds)]
    assert sent[0].url.host == "8.8.8.8"
    assert sent[0].headers["host"] == sent[0].extensions["sni_hostname"] == "native.example"
    assert sent[0].headers["authorization"] == "Bearer " + CANARY
    assert (
        sent[0].extensions["timeout"]["read"]
        == settings.media_execution_policy.call_timeout_seconds
    )
