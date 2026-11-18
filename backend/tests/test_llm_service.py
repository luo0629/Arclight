"""Exercise actual ChatModels and vendor SDKs without opening a vendor connection."""

import base64
import gzip
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tracers.langchain import LangChainTracer
from pydantic import SecretStr
from test_agent_model import FinalResult, final_response, model_request
from test_asset_versions import image_bytes

from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities, ContextImage
from arclight.domain.model_gateway import GatewayFailure
from arclight.providers import llm_transport, network
from arclight.providers.agent_model import OpenAICompatibleAgentProvider
from arclight.providers.llm import LLMService, dispatch_inference
from arclight.providers.text import OpenAICompatibleTextProvider

PROTOCOLS = ["openai_chat", "gemini", "qwen_chat"]
KEY = "synthetic-frozen-credential"


def response(protocol, content='{"summary":"fixture","requires_human_review":false}'):
    if protocol == "gemini":
        return {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 5},
        }
    choices = [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]
    if protocol == "qwen_chat":
        return {"output": {"choices": choices}, "usage": {"input_tokens": 11, "output_tokens": 5}}
    return {"choices": choices, "usage": {"prompt_tokens": 11, "completion_tokens": 5}}


def turn(service, settings, identity=("SERVER", "fixture-actor")):
    return dispatch_inference(
        settings,
        identity,
        lambda: service.invoke(
            settings,
            [SystemMessage("private-instruction"), HumanMessage("private-input")],
            max_tokens=20,
            response_format={"type": "json_object"},
        ),
    )


def test_no_live_inference_without_gateway_admission(settings, monkeypatch):
    settings.llm_agent_capabilities = AgentModelCapabilities(
        version="test:1",
        model=settings.llm_model,
        supplier=settings.llm_supplier,
        response_formats=frozenset({"json_schema"}),
    )
    monkeypatch.setattr(llm_transport, "PublicHTTPTransport", lambda *_: pytest.fail("opened pool"))
    for call in (
        lambda: OpenAICompatibleTextProvider(settings).analyze("unadmitted material"),
        lambda: OpenAICompatibleAgentProvider(settings).complete(
            model_request(), (), FinalResult, {}
        ),
    ):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == "GATEWAY_ADMISSION_REQUIRED"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_arbitrary_image_urls_are_not_passed_to_sdks(settings, protocol, stub_llm):
    settings.llm_protocol = protocol
    service = stub_llm(lambda _: pytest.fail("uncontrolled image reached SDK transport"))
    with pytest.raises(GatewayFailure) as caught:
        dispatch_inference(
            settings,
            ("SERVER", "test"),
            lambda: service.invoke(
                settings,
                [
                    HumanMessage(
                        [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://untrusted.example/private.png"},
                            }
                        ]
                    )
                ],
                max_tokens=20,
                response_format={"type": "json_object"},
            ),
        )
    assert caught.value.code == "INPUT_INVALID" and caught.value.not_accepted


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_sdk_uses_guarded_pinned_transport_and_ignores_environment(
    settings, protocol, monkeypatch, caplog
):
    ports, pools, dns, sent = [], [], [], []

    def resolve(host, port, **kwargs):
        dns.append(host)
        ports.append(port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    def create_transport(dns_timeout):
        pool = network.PublicHTTPTransport(dns_timeout)
        pool.inner.close()

        def respond(request):
            sent.append((len(pools), request))
            return httpx.Response(200, json=response(protocol), headers={"set-cookie": "private=1"})

        pool.inner = httpx.MockTransport(respond)
        pools.append(pool)
        return pool

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(llm_transport, "PublicHTTPTransport", create_transport)
    monkeypatch.setattr(
        LangChainTracer, "__init__", lambda *_a, **_k: pytest.fail("tracer created")
    )
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "GEMINI_API_KEY",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_WORKSPACE",
    ):
        monkeypatch.setenv(name, "ignored-environment-value")
    monkeypatch.setenv("OPENAI_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.set_level("DEBUG")
    settings.llm_protocol = protocol
    settings.llm_model = "models/exact/model" if protocol == "gemini" else "o1-exact-model"
    settings.llm_base_url = "https://one.example/custom/v3"
    settings.llm_api_key = SecretStr(KEY)
    service = LLMService()
    try:
        for config in (
            settings,
            settings,
            settings.model_copy(
                update={
                    "llm_base_url": "https://two.example/custom/v3",
                }
            ),
        ):
            result, usage = turn(service, config)
            assert result.reason == "stop" and usage.input_tokens == 11 and usage.output_tokens == 5
        assert len(pools) == 2 and len(sent) == 3
        assert dns == ["one.example", "one.example", "two.example"] and ports == [443] * 3
        path = {
            "openai_chat": b"/chat/completions",
            "gemini": b"/models/exact%2Fmodel:generateContent",
            "qwen_chat": b"/services/aigc/text-generation/generation",
        }[protocol]
        for host, (_, request) in zip(dns, sent, strict=True):
            assert request.url.host == "8.8.8.8" and request.url.raw_path == b"/custom/v3" + path
            assert request.headers["host"] == host == request.extensions["sni_hostname"]
            assert request.headers["accept-encoding"] == "identity"
            assert "cookie" not in request.headers
            assert (
                "openai-organization" not in request.headers
                and "openai-project" not in request.headers
            )
            assert "x-dashscope-workspace" not in request.headers
            if protocol == "gemini":
                assert request.headers["x-goog-api-key"] == KEY
            else:
                assert request.headers["authorization"] == f"Bearer {KEY}"
                wire = json.loads(request.content)
                assert wire["model"] == settings.llm_model
                if protocol == "openai_chat":
                    assert wire["messages"][0] == {
                        "role": "system",
                        "content": "private-instruction",
                    }
    finally:
        service.clear()
    assert all(value not in caplog.text for value in (KEY, "private-input", "private-instruction"))


@pytest.mark.parametrize("protocol", ["gemini", "qwen_chat"])
def test_native_vision_preserves_inline_images(protocol, settings, stub_llm):
    settings.llm_protocol = protocol
    settings.llm_agent_capabilities = AgentModelCapabilities(
        version="test:1",
        model=settings.llm_model,
        supplier=settings.llm_supplier,
        max_input_images=2,
        response_formats=frozenset({"json_schema"}),
    )
    identity = uuid4()
    png = image_bytes()
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=response(protocol))

    provider = OpenAICompatibleAgentProvider(settings, service=stub_llm(handler))
    result = provider.complete(
        model_request(
            messages=[
                {
                    "role": "user",
                    "image_file_ids": [identity, identity],
                }
            ]
        ),
        (),
        FinalResult,
        {identity: ContextImage(identity, "image/png", png)},
    )
    assert result.status == "FINAL" and len(sent) == 1
    request = sent[0]
    body = json.loads(request.content)
    encoded = base64.b64encode(png).decode()
    if protocol == "gemini":
        assert [
            part["inlineData"] for part in body["contents"][0]["parts"] if "inlineData" in part
        ] == [
            {"mimeType": "image/png", "data": encoded},
        ] * 2
    else:
        assert request.url.path.endswith(llm_transport.QWEN_VISUAL)
        assert body["parameters"] == {
            "enable_thinking": False,
            "max_tokens": 4096,
            "result_format": "message",
            "response_format": {"type": "json_object"},
        }
        assert [
            part["image"] for part in body["input"]["messages"][-1]["content"] if "image" in part
        ] == [
            f"data:image/png;base64,{encoded}",
        ] * 2


@pytest.mark.parametrize("problem", ["redirect", "timeout", "wire", "inflated", "within_limit"])
def test_sdk_does_not_resend_and_limits_encoded_and_decoded_bodies(settings, stub_llm, problem):
    sent, closed = [], []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if problem == "wire":
                yield b" " * (llm_transport.RESPONSE_LIMIT + 1)
            else:
                data = json.dumps(final_response()).encode()
                if problem == "inflated":
                    data += b" " * llm_transport.RESPONSE_LIMIT
                yield gzip.compress(data)

        def close(self):
            closed.append(True)

    def respond(request):
        sent.append(request)
        if problem == "timeout":
            raise httpx.ReadTimeout("private-timeout-detail")
        if problem == "redirect":
            return httpx.Response(
                307, headers={"Location": "https://redirect.example/"}, stream=Stream()
            )
        return httpx.Response(
            200,
            stream=Stream(),
            headers={
                "content-encoding": "identity" if problem == "wire" else "gzip",
            },
        )

    service = stub_llm(respond)
    if problem == "within_limit":
        assert turn(service, settings)[0].reason == "stop"
    else:
        with pytest.raises(GatewayFailure) as error:
            turn(service, settings)
        assert error.value.code == {
            "redirect": "SERVICE_REDIRECT_BLOCKED",
            "timeout": "PROVIDER_UNAVAILABLE",
        }.get(problem, "INVALID_RESPONSE")
        assert not error.value.not_accepted and not error.value.completed
        assert "private-timeout-detail" not in str(error.value)
    assert len(sent) == 1 and len(closed) == (0 if problem == "timeout" else 1)


@pytest.mark.parametrize("billed", [False, True])
def test_dashscope_200_business_rejection_and_usage(settings, stub_llm, billed):
    settings.llm_protocol = "qwen_chat"
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": "Arrearage",
                "message": "private-qwen-error",
                "usage": {"input_tokens": 9, "output_tokens": 1} if billed else {},
            },
        )

    with pytest.raises(GatewayFailure) as caught:
        turn(stub_llm(respond), settings)
    error = caught.value
    assert len(calls) == 1 and error.code == "PROVIDER_QUOTA_EXHAUSTED"
    assert error.completed is billed and error.not_accepted is not billed
    assert error.usage.input_tokens == (9 if billed else None) and error.usage.actual_cost is None
    assert "private-qwen-error" not in str(error)


def test_pool_isolated_by_credential_and_evicts_only_after_inflight_release(settings, monkeypatch):
    settings.llm_api_key = SecretStr(KEY)
    entered, release = threading.Event(), threading.Event()
    created, closed = [], []

    class Transport(httpx.MockTransport):
        def __init__(self):
            self.index = len(created)
            created.append(self.index)
            super().__init__(self.respond)

        def respond(self, request):
            if self.index == 0 and not entered.is_set():
                entered.set()
                assert release.wait(15)
            assert request.headers["authorization"].startswith("Bearer synthetic-")
            return httpx.Response(200, json=final_response())

        def close(self):
            closed.append(self.index)

    monkeypatch.setattr(llm_transport, "PublicHTTPTransport", lambda *_: Transport())
    service = LLMService(capacity=1)
    identity = ("ACCOUNT_SNAPSHOT", "account-1", "version-1")
    with ThreadPoolExecutor(2) as executor:
        active = executor.submit(turn, service, settings, identity)
        try:
            assert entered.wait(15)
            assert executor.submit(turn, service, settings, identity).result(15)[0].reason == "stop"
            assert created == [0]
            turn(service, settings, ("ACCOUNT_SNAPSHOT", "account-2", "version-1"))
            assert created == [0, 1] and not closed
            rotated = settings.model_copy(update={"llm_api_key": SecretStr("synthetic-rotated")})
            turn(service, rotated, ("ACCOUNT_SNAPSHOT", "account-2", "version-1"))
            assert created == [0, 1, 2] and closed == [1]
        finally:
            release.set()
        assert active.result(15)[0].reason == "stop"
    assert closed == [1, 0]
    service.clear()
    assert closed == [1, 0, 2]
