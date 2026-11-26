import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from arclight.domain.model_gateway import GatewayFailure
from arclight.providers.model_capability_probe import probe_model_capabilities

_COLORS = {
    (0, 0, 0): "black",
    (255, 255, 255): "white",
    (255, 0, 0): "red",
    (0, 255, 0): "green",
    (0, 0, 255): "blue",
    (255, 255, 0): "yellow",
    (255, 0, 255): "magenta",
    (0, 255, 255): "cyan",
}
_IDENTITY = ("ACCOUNT_CAPABILITY_PROBE", "synthetic-account")


@pytest.fixture
def probe_settings(settings):
    return settings.model_copy(
        update={
            "provider": "openai_compatible",
            "llm_supplier": "custom",
            "llm_protocol": "openai_chat",
            "llm_model": "test-agent-model",
            "llm_base_url": "https://example.com/v1",
            "llm_api_key": SecretStr("synthetic-model-capability-probe-key"),
            "llm_agent_capabilities": None,
        }
    )


def _response(reason, **message):
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": reason, "message": {"role": "assistant", **message}}],
        },
    )


def _user_tiles(body):
    content = body["messages"][1]["content"]
    if isinstance(content, str):
        assert json.loads(content) == {"trust": "UNTRUSTED", "data": {"probe": True}}
        return []
    colors = []
    for part in content:
        if part["type"] == "image_url":
            data_url = part["image_url"]["url"]
            header, data = data_url.split(",", maxsplit=1)
            assert header == "data:image/png;base64"
            with Image.open(BytesIO(base64.b64decode(data))) as tile:
                assert tile.size == (64, 64)
                colors.append(_COLORS[tile.getpixel((32, 32))])
    return colors


def _choose_tool(colors):
    return _response(
        "tool_calls",
        content=None,
        tool_calls=[
            {
                "id": "synthetic_probe_call",
                "type": "function",
                "function": {
                    "name": "probe_receipt",
                    "arguments": json.dumps({"colors": colors}),
                },
            }
        ],
    )


def _answer(body, *, invalid_type=False):
    receipt = body["messages"][-1]
    assert receipt["role"] == "tool"
    assert receipt["tool_call_id"] == "synthetic_probe_call"
    feedback = json.loads(receipt["content"])
    assert feedback["tool_name"] == "probe_receipt" and feedback["status"] == "SUCCEEDED"
    value = feedback["result"]["value"]
    return _response("stop", content=json.dumps({"value": str(value) if invalid_type else value}))


def test_probe_verifies_tool_feedback_and_eight_images_with_production_transport(
    probe_settings, stub_llm
):
    observed = []

    def upstream(request):
        body = json.loads(request.content)
        colors = _user_tiles(body)
        observed.append((body["response_format"]["type"], colors, len(body["messages"])))
        assert str(request.url) == "https://example.com/v1/chat/completions"
        assert body["model"] == "test-agent-model"
        assert [tool["function"]["name"] for tool in body["tools"]] == ["probe_receipt"]
        return _choose_tool(colors) if len(body["messages"]) == 2 else _answer(body)

    stub_llm(upstream)
    capabilities = probe_model_capabilities(probe_settings, _IDENTITY)

    assert capabilities is not None
    assert capabilities.model == probe_settings.llm_model
    assert capabilities.supplier == probe_settings.llm_supplier
    assert capabilities.response_formats == {"json_schema"}
    assert capabilities.tools is True
    assert capabilities.max_input_images == 8
    assert len(observed) == 4
    assert observed[0] == ("json_schema", [], 2)
    assert observed[1] == ("json_schema", [], 4)
    assert set(observed[2][1]) == set(_COLORS.values())
    assert observed[3] == ("json_schema", observed[2][1], 4)
    assert probe_settings.llm_agent_capabilities is None


@pytest.mark.parametrize("schema_failure", ["request_rejected", "wrong_final_type"])
def test_probe_falls_back_to_verified_json_object_and_never_grants_unverified_vision(
    probe_settings, stub_llm, schema_failure
):
    observed = []

    def upstream(request):
        body = json.loads(request.content)
        response_format = body["response_format"]["type"]
        colors = _user_tiles(body)
        observed.append((response_format, len(colors)))
        if colors:
            return _choose_tool(colors[:1])
        if response_format == "json_schema" and schema_failure == "request_rejected":
            return httpx.Response(400, json={"error": {"message": "sentinel vendor text"}})
        if len(body["messages"]) == 2:
            return _choose_tool([])
        return _answer(body, invalid_type=response_format == "json_schema")

    stub_llm(upstream)
    capabilities = probe_model_capabilities(probe_settings, _IDENTITY)

    schema_calls = 1 if schema_failure == "request_rejected" else 2
    assert capabilities is not None
    assert capabilities.response_formats == {"json_object"}
    assert capabilities.max_input_images == 0
    assert observed == [
        *([("json_schema", 0)] * schema_calls),
        ("json_object", 0),
        ("json_object", 0),
        ("json_object", 8),
    ]


def test_probe_rejects_valid_json_without_tool_roundtrip(probe_settings, stub_llm):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content)["response_format"]["type"])
        return _response("stop", content='{"value":0}')

    stub_llm(upstream)

    assert probe_model_capabilities(probe_settings, _IDENTITY) is None
    assert calls == ["json_schema", "json_object", "json_schema", "json_object"]


def test_probe_can_verify_plain_text_format_without_granting_tools(probe_settings, stub_llm):
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        response_format = body["response_format"]["type"]
        has_tools = bool(body.get("tools"))
        calls.append((response_format, has_tools))
        if has_tools or response_format == "json_schema":
            return httpx.Response(422, json={"error": {"message": "invalid combination"}})
        value = json.loads(body["messages"][1]["content"])["data"]["value"]
        return _response("stop", content=json.dumps({"value": value}))

    stub_llm(upstream)
    capabilities = probe_model_capabilities(probe_settings, _IDENTITY)

    assert capabilities is not None
    assert capabilities.response_formats == {"json_object"}
    assert capabilities.tools is False
    assert capabilities.max_input_images == 0
    assert calls == [
        ("json_schema", True),
        ("json_object", True),
        ("json_schema", False),
        ("json_object", False),
    ]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PROVIDER_AUTHENTICATION_FAILED"),
        (402, "PROVIDER_INSUFFICIENT_BALANCE"),
        (403, "PROVIDER_ACCESS_DENIED"),
        (429, "PROVIDER_RATE_LIMITED"),
        (500, "PROVIDER_UNAVAILABLE"),
        (None, "PROVIDER_UNAVAILABLE"),
    ],
)
def test_probe_stops_without_further_calls_on_unavailable_or_unknown_outcome(
    probe_settings, stub_llm, status, code
):
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body["response_format"]["type"])
        if len(calls) == 1:
            return _choose_tool([])
        if status is None:
            raise httpx.ReadTimeout("sentinel vendor text", request=request)
        return httpx.Response(status, json={"error": {"message": "sentinel vendor text"}})

    stub_llm(upstream)

    with pytest.raises(GatewayFailure) as caught:
        probe_model_capabilities(probe_settings, _IDENTITY)
    assert caught.value.code == code
    assert "sentinel vendor text" not in str(caught.value)
    assert calls == ["json_schema", "json_schema"]
