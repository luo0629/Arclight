"""Actual native wire adapters exercised with HTTP transports and private objects."""

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr
from test_agent_model import FinalResult, declaration, model_request
from test_asset_versions import image_bytes

from arclight.core.errors import DomainError
from arclight.domain.agent_model import (
    AgentAssistantMessage,
    AgentModelCapabilities,
    AgentToolMessage,
)
from arclight.domain.agent_tools import AgentToolFeedback
from arclight.domain.generation_requests import GenerationSpec
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.versions import digest
from arclight.providers import native_media_protocols
from arclight.providers.agent_model import OpenAICompatibleAgentProvider
from arclight.providers.media_query import ProviderLookup
from arclight.providers.media_submission import MediaImageInput, MediaSubmission
from arclight.providers.native_media import MediaConnection, NativeMediaProvider
from arclight.providers.native_media_protocols import ASYNC, KINDS
from arclight.providers.native_media_protocols import submission as media_submission

CANARY = "native-http-private-key"


@pytest.mark.parametrize(
    "protocol,model,duration,height,valid",
    [
        ("cogvideo", "cogvideox-3", 6, 720, False),
        ("cogvideo", "cogvideox-3", 5, 720, True),
        ("cogvideo", "cogvideox-flash", 5, 720, False),
        ("cogvideo", "cogvideox-flash", 5, 1080, True),
        ("seedance", "doubao-seedance-1-0-pro", 13, 720, False),
        ("seedance", "doubao-seedance-1-0-pro", 2, 720, True),
        ("seedance", "doubao-seedance-2-0", 2, 720, False),
        ("seedance", "doubao-seedance-2-0", 4, 720, True),
        ("seedance", "doubao-seedance-2-0-fast", 6, 1080, False),
        ("seedance", "doubao-seedance-2-0-fast", 6, 720, True),
        ("wan", "wan2.1-t2v-turbo", 10, 720, False),
        ("wan", "wan2.1-t2v-turbo", 5, 720, True),
        ("wan", "wan2.5-t2v-preview", 15, 720, False),
        ("wan", "wan2.5-t2v-preview", 10, 720, True),
        ("wan", "wan2.6-t2v", 15, 720, True),
        ("wan", "wan2.7-t2v", 2, 720, True),
        ("wan", "wan2.7-t2v", 1, 720, False),
        ("veo", "veo-3.1-generate-preview", 6, 1080, False),
        ("veo", "veo-3.1-generate-preview", 8, 1080, True),
    ],
)
def test_native_video_rejects_known_model_parameter_mismatches_before_io(
    protocol, model, duration, height, valid
):
    spec = GenerationSpec(
        provider=protocol,
        model=model,
        model_config_version="test-config:1",
        prompt="synthetic video",
        prompt_version="test:1",
        output_spec={
            "width": 1920 if height == 1080 else 1280,
            "height": height,
            "duration_seconds": duration,
            "format": "mp4",
        },
    )
    if not valid:
        with pytest.raises(DomainError) as error:
            media_submission(spec)
        assert error.value.code in {"PROVIDER_DURATION_UNSUPPORTED", "PROVIDER_SIZE_UNSUPPORTED"}
    else:
        _, body, _ = media_submission(spec)
        if model == "cogvideox-flash":
            assert "duration" not in body


def native_request(settings, tmp_path, protocol):
    settings.asset_storage_path = tmp_path / "native-objects"
    settings.asset_storage_path.mkdir(exist_ok=True)
    task = KINDS[protocol]
    configuration = {
        "provider": protocol,
        "protocol": protocol,
        "model": "native-model",
        "base_url": "https://api.example/v1",
    }
    configuration["configuration_version"] = digest(configuration)
    output = (
        {"width": 1024, "height": 1024, "format": "png"}
        if task == "IMAGE"
        else (
            {
                "width": 1280,
                "height": 720,
                "duration_seconds": 5 if protocol in {"wan", "cogvideo"} else 6,
                "format": "mp4",
            }
            if task == "VIDEO"
            else {"format": "wav"}
        )
    )
    spec = GenerationSpec(
        provider=protocol,
        model="native-model",
        model_config_version=configuration["configuration_version"],
        prompt="synthetic prompt",
        prompt_version="test:1",
        output_spec=output,
        parameters={"voice": "test-voice"} if task == "AUDIO" else {},
    )
    request = MediaSubmission(
        uuid4(),
        uuid4(),
        uuid4(),
        {
            "context": {
                "generation": spec.model_dump(mode="json"),
                "provider_configuration": configuration,
            }
        },
        None,
    )
    return MediaConnection(configuration, CANARY), request


@pytest.mark.parametrize(
    "protocol", ["openai_image", "cogview", "seedream", "siliconflow_image", "gemini_image"]
)
def test_native_images_wire_private_download_and_durable_handle(settings, tmp_path, protocol):
    connection, request = native_request(settings, tmp_path, protocol)
    data = image_bytes("PNG", (1024, 1024))
    calls = []

    def handler(req):
        calls.append(req)
        if req.method == "GET":
            assert str(req.url) == "https://cdn.example/image?signature=ephemeral-secret"
            assert "authorization" not in req.headers and "x-goog-api-key" not in req.headers
            return httpx.Response(200, content=data)
        body = json.loads(req.content)
        if protocol == "gemini_image":
            assert req.url.path == "/v1/models/native-model:generateContent"
            assert req.headers["x-goog-api-key"] == CANARY
            assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "1:1"
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "data": base64.b64encode(data).decode(),
                                            "mimeType": "image/png",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        assert req.url.path == "/v1/images/generations"
        assert req.headers["authorization"] == "Bearer " + CANARY
        assert body["model"] == "native-model"
        assert body.get("image_size" if protocol == "siliconflow_image" else "size") == "1024x1024"
        if protocol == "openai_image":
            assert body["n"] == 1 and body["output_format"] == "png"
        if protocol == "seedream":
            assert body["response_format"] == "b64_json"
        field = "images" if protocol == "siliconflow_image" else "data"
        return httpx.Response(
            200, json={field: [{"url": "https://cdn.example/image?signature=ephemeral-secret"}]}
        )

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    receipt = provider.submit(request)
    assert receipt.status == "SUCCEEDED" and receipt.result is not None
    serialized = receipt.model_dump_json()
    assert CANARY not in serialized and "ephemeral-secret" not in serialized
    handle = receipt.result.output_handles[0]
    assert provider.download(request, handle, 1024**2).data == data
    other = MediaSubmission(
        uuid4(), request.attempt_id, request.input_manifest_id, request.snapshot, None
    )
    with pytest.raises(DomainError):
        provider.download(other, handle, 1024**2)
    assert len(calls) in {1, 2}


@pytest.mark.parametrize("protocol", sorted(ASYNC))
def test_async_submit_query_and_cancel_are_authoritative(settings, tmp_path, protocol):
    connection, request = native_request(settings, tmp_path, protocol)
    calls = []

    def handler(req):
        calls.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            assert body.get("model", "native-model") == "native-model"
            if protocol == "wan":
                assert req.headers["x-dashscope-async"] == "enable"
                assert body["parameters"]["size"] == "1280*720"
                return httpx.Response(200, json={"output": {"task_id": "task_1"}})
            if protocol == "veo":
                assert body["parameters"]["durationSeconds"] == 6
                return httpx.Response(200, json={"name": "models/native-model/operations/op_1"})
            return httpx.Response(
                200, json={"request_id" if protocol == "xai_video" else "id": "task_1"}
            )
        assert req.method == "GET"
        if protocol == "veo":
            assert req.url.path.endswith("/models/native-model/operations/op_1")
            return httpx.Response(200, json={"done": True, "error": {"message": CANARY}})
        return httpx.Response(
            200,
            json={"output": {"task_status": "FAILED"}}
            if protocol == "wan"
            else {"task_status" if protocol == "cogvideo" else "status": "failed"},
        )

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    submitted = provider.submit(request)
    assert submitted.status == "SUBMITTED" and submitted.request_id
    lookup = ProviderLookup(protocol, "native-model", submitted.request_id, request)
    assert provider.query(lookup).status == "FAILED"
    assert provider.cancel(lookup).status == "UNSUPPORTED"
    assert provider.capabilities.supports_query and not provider.capabilities.supports_cancel
    assert len(calls) == 2


@pytest.mark.parametrize("protocol", ["openai_tts", "siliconflow_tts", "qwen_tts"])
def test_native_speech_request_and_private_audio(settings, tmp_path, protocol):
    connection, request = native_request(settings, tmp_path, protocol)
    audio = b"RIFF" + bytes(4) + b"WAVE" + b"synthetic protocol bytes"

    def handler(req):
        if req.method == "GET":
            assert "authorization" not in req.headers
            return httpx.Response(200, content=audio)
        body = json.loads(req.content)
        assert body["model"] == "native-model"
        if protocol == "qwen_tts":
            assert req.url.path.endswith("/services/aigc/multimodal-generation/generation")
            assert body["input"] == {"text": "synthetic prompt", "voice": "test-voice"}
            return httpx.Response(
                200,
                json={
                    "output": {"audio": {"url": "https://cdn.example/speech"}},
                    "usage": {"characters": 16},
                },
            )
        assert body["voice"] == "test-voice" and body["response_format"] == "wav"
        return httpx.Response(
            200, content=audio, headers={"Content-Type": "application/octet-stream"}
        )

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    receipt = provider.submit(request)
    assert receipt.status == "SUCCEEDED" and receipt.result
    assert provider.download(request, receipt.result.output_handles[0], 1024).data == audio


@pytest.mark.parametrize(
    "model,parameters,accepted",
    [
        ("tts-1", {"voice": "alloy", "instructions": "Speak calmly."}, False),
        ("tts-1-hd", {"voice": "alloy", "instructions": "Speak calmly."}, False),
        ("tts-1", {"voice": "alloy", "speed": 0.8}, True),
        ("tts-1-hd", {"voice": "alloy", "speed": 1.2}, True),
        ("gpt-4o-mini-tts", {"voice": "alloy", "instructions": "Speak calmly."}, True),
        ("tts-1-compatible", {"voice": "alloy", "instructions": "Speak calmly."}, True),
    ],
)
def test_native_tts_model_parameters_are_validated_before_wire_submission(
    model, parameters, accepted
):
    spec = GenerationSpec(
        provider="openai_tts",
        model=model,
        model_config_version="test-config:1",
        prompt="Original test dialogue.",
        prompt_version="test:1",
        parameters=parameters,
        output_spec={"format": "wav"},
    )
    if not accepted:
        with pytest.raises(DomainError) as error:
            media_submission(spec)
        assert error.value.code == "PROVIDER_PARAMETERS_UNSUPPORTED"
        return
    path, body, _ = media_submission(spec)
    assert path == "/audio/speech"
    assert body == {
        "model": model,
        "input": spec.prompt,
        "response_format": "wav",
        **parameters,
    }


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("FunAudioLLM/CosyVoice2-0.5B", id="cosyvoice2"),
        pytest.param("fnlp/MOSS-TTSD-v0.5", id="moss-ttsd"),
    ],
)
@pytest.mark.parametrize(
    "output,accepted",
    [
        pytest.param({"format": "flac"}, False, id="flac"),
        pytest.param({"format": "wav", "sample_rate": 48000}, False, id="wav-48000"),
        pytest.param({"format": "mp3", "sample_rate": 24000}, False, id="mp3-24000"),
        pytest.param({"format": "opus", "sample_rate": 44100}, False, id="opus-44100"),
        pytest.param({"format": "wav", "sample_rate": 44100}, True, id="wav-44100"),
        pytest.param({"format": "mp3", "sample_rate": 32000}, True, id="mp3-32000"),
        pytest.param({"format": "opus", "sample_rate": 48000}, True, id="opus-48000"),
        pytest.param({"format": "wav"}, True, id="wav-native-rate"),
        pytest.param({"format": "mp3"}, True, id="mp3-native-rate"),
        pytest.param({"format": "opus"}, True, id="opus-native-rate"),
    ],
)
def test_siliconflow_known_speech_models_validate_format_and_rate_before_submission(
    model, output, accepted
):
    spec = GenerationSpec(
        provider="siliconflow_tts",
        model=model,
        model_config_version="test-config:1",
        prompt="Original test dialogue.",
        prompt_version="test:1",
        parameters={"voice": f"{model}:alex", "speed": 1.0},
        output_spec=output,
    )
    if not accepted:
        with pytest.raises(DomainError) as error:
            media_submission(spec)
        assert error.value.code == "PROVIDER_PARAMETERS_UNSUPPORTED"
        return
    path, body, _ = media_submission(spec)
    assert path == "/audio/speech"
    assert body == {
        "model": model,
        "input": spec.prompt,
        "voice": f"{model}:alex",
        "speed": 1.0,
        "response_format": output["format"],
        **({"sample_rate": output["sample_rate"]} if "sample_rate" in output else {}),
    }


def test_siliconflow_unknown_compatible_speech_model_keeps_protocol_output_contract():
    spec = GenerationSpec(
        provider="siliconflow_tts",
        model="FunAudioLLM/CosyVoice2-0.5B-compatible",
        model_config_version="test-config:1",
        prompt="Original test dialogue.",
        prompt_version="test:1",
        parameters={"voice": "custom-voice"},
        output_spec={"format": "flac", "sample_rate": 48000},
    )
    path, body, _ = media_submission(spec)
    assert path == "/audio/speech"
    assert body["response_format"] == "flac"
    assert body["sample_rate"] == 48000
    # Container safety is checked separately by the existing isolated parser before registration.


@pytest.mark.parametrize("protocol", ["gemini", "qwen_chat"])
def test_native_agent_structured_output_tools_and_usage(settings, stub_llm, protocol):
    settings.llm_protocol = protocol
    settings.llm_base_url = "https://api.example/v1"
    settings.llm_api_key = SecretStr(CANARY)
    settings.llm_model = "native-model"
    settings.llm_supplier = "google" if protocol == "gemini" else "qwen"
    settings.llm_agent_capabilities = AgentModelCapabilities(
        version="native:1",
        supplier=settings.llm_supplier,
        model=settings.llm_model,
        tools=True,
        response_formats=frozenset({"json_schema", "json_object"}),
        max_input_images=0,
    )
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        body = json.loads(req.content)
        content = '{"summary":"complete","requires_human_review":false}'
        if protocol == "gemini":
            assert req.url.path.endswith("/models/native-model:generateContent")
            assert req.headers["x-goog-api-key"] == CANARY
            if count == 1:
                assert body["tools"][0]["functionDeclarations"][0]["name"] == "read_story_entry"
                parts = [
                    {
                        "functionCall": {
                            "id": "native-call-1",
                            "name": "read_story_entry",
                            "args": {"entry_id": str(uuid4())},
                        },
                        "thoughtSignature": "opaque-native-signature",
                    }
                ]
            else:
                assert body["generationConfig"]["responseMimeType"] == "application/json"
                parts = body["contents"][1]["parts"]
                assert parts[0]["thoughtSignature"] == "opaque-native-signature"
                assert parts[0]["functionCall"]["id"] == "native-call-1"
                feedback = body["contents"][2]["parts"][0]["functionResponse"]
                assert feedback["id"] == "native-call-1"
                assert feedback["name"] == "read_story_entry"
                parts = [{"text": content}]
            return httpx.Response(
                200,
                json={
                    "candidates": [{"finishReason": "STOP", "content": {"parts": parts}}],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
                },
            )
        assert req.url.path.endswith("/services/aigc/text-generation/generation")
        assert req.headers["authorization"] == "Bearer " + CANARY
        parameters = body["parameters"]
        assert parameters["result_format"] == "message"
        if count == 2:
            assert body["input"]["messages"][-1]["tool_call_id"] == "call_qwen"
        message = (
            {
                "tool_calls": [
                    {
                        "id": "call_qwen",
                        "type": "function",
                        "function": {
                            "name": "read_story_entry",
                            "arguments": json.dumps({"entry_id": str(uuid4())}),
                        },
                    }
                ]
            }
            if count == 1
            else {"content": content}
        )
        return httpx.Response(
            200,
            json={
                "output": {
                    "choices": [
                        {
                            "finish_reason": "tool_calls" if count == 1 else "stop",
                            "message": message,
                        }
                    ]
                },
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    provider = OpenAICompatibleAgentProvider(settings, service=stub_llm(handler))
    request = model_request()
    tools = provider.complete(request, (declaration(),), FinalResult, {})
    assert tools.status == "TOOL_CALLS" and len(tools.tool_calls) == 1
    call = tools.tool_calls[0]
    if protocol == "gemini":
        assert call.provider_signature == "opaque-native-signature"
        assert call.provider_call_id == "native-call-1"
    request.messages.extend(
        [
            AgentAssistantMessage(tool_calls=[call]),
            AgentToolMessage(
                call_id=call.call_id,
                feedback=AgentToolFeedback(
                    run_id=request.run_id,
                    tool_name=call.tool_name,
                    idempotency_key=call.call_id,
                    status="SUCCEEDED",
                    result={},
                ),
            ),
        ]
    )
    final = provider.complete(request, (), FinalResult, {})
    assert final.status == "FINAL" and final.final.summary == "complete"
    assert provider.last_usage.input_tokens == 12 and provider.last_usage.output_tokens == 4


@pytest.mark.parametrize("protocol", ["gemini", "qwen_chat"])
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PROVIDER_AUTHENTICATION_FAILED"),
        (402, "PROVIDER_INSUFFICIENT_BALANCE"),
        (403, "PROVIDER_ACCESS_DENIED"),
        (429, "PROVIDER_RATE_LIMITED"),
    ],
)
def test_native_errors_are_sanitized(settings, stub_llm, protocol, status, code):
    settings.llm_protocol = protocol
    settings.llm_api_key = SecretStr(CANARY)
    service = stub_llm(lambda _req: httpx.Response(status, json={"error": {"message": CANARY}}))
    with pytest.raises(GatewayFailure) as error:
        service.invoke(
            settings,
            [HumanMessage("synthetic")],
            response_format={"type": "json_object"},
        )
    assert error.value.code == code and error.value.not_accepted
    assert CANARY not in str(error.value)


@pytest.mark.parametrize("protocol", ["gemini", "qwen_chat"])
@pytest.mark.parametrize("malformed", [None, [None], {"content": None}])
def test_completed_malformed_native_text_retains_known_usage(
    settings, stub_llm, protocol, malformed
):
    settings.llm_protocol = protocol
    body = (
        {
            "candidates": malformed,
            "usageMetadata": {"promptTokenCount": 19, "candidatesTokenCount": 7},
        }
        if protocol == "gemini"
        else {"output": malformed, "usage": {"input_tokens": 19, "output_tokens": 7}}
    )
    service = stub_llm(lambda req: httpx.Response(200, json=body))
    with pytest.raises(GatewayFailure) as error:
        service.invoke(
            settings,
            [HumanMessage("test")],
            response_format={"type": "json_object"},
        )
    assert error.value.completed and not error.value.not_accepted
    assert error.value.usage.input_tokens == 19 and error.value.usage.output_tokens == 7


@pytest.mark.parametrize("protocol", sorted(ASYNC))
def test_async_success_download_uses_original_request_and_sanitizes_urls(
    settings, tmp_path, protocol
):
    connection, request = native_request(settings, tmp_path, protocol)
    video = b"\x00\x00\x00\x14ftypisom" + bytes(8)

    def handler(req):
        if req.url.host == "cdn.example":
            assert not any(name in req.headers for name in ("authorization", "x-goog-api-key"))
            return httpx.Response(200, content=video)
        source = "https://cdn.example/video?signature=ephemeral"
        body = {
            "seedance": {"status": "succeeded", "content": {"video_url": source}},
            "cogvideo": {"task_status": "SUCCESS", "video_result": [{"url": source}]},
            "wan": {"output": {"task_status": "SUCCEEDED", "video_url": source}},
            "xai_video": {"status": "done", "video": {"url": source}},
            "veo": {
                "done": True,
                "response": {
                    "generateVideoResponse": {"generatedSamples": [{"video": {"uri": source}}]}
                },
            },
        }[protocol]
        return httpx.Response(200, json=body)

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    identity = (
        "op_" + base64.urlsafe_b64encode(b"operations/example").decode().rstrip("=")
        if protocol == "veo"
        else "task_1"
    )
    result = provider.query(ProviderLookup(protocol, "native-model", identity, request))
    assert result.status == "SUCCEEDED" and result.result
    assert "ephemeral" not in result.model_dump_json()
    assert provider.download(request, result.result.output_handles[0], 1024).data == video


def test_download_rejection_cannot_mark_completed_generation_free(settings, tmp_path):
    connection, request = native_request(settings, tmp_path, "cogview")

    def handler(req):
        return (
            httpx.Response(401)
            if req.method == "GET"
            else httpx.Response(200, json={"data": [{"url": "https://cdn.example/result"}]})
        )

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayFailure) as error:
        provider.submit(request)
    assert error.value.completed and not error.value.not_accepted
    assert error.value.usage.images == 1 and error.value.usage.actual_cost is None


def test_wan27_uses_current_native_resolution_and_ratio():
    from arclight.providers.native_media_protocols import submission

    spec = GenerationSpec(
        provider="wan",
        model="wan2.7-t2v",
        model_config_version="v1",
        prompt="synthetic",
        prompt_version="v1",
        output_spec={"width": 1280, "height": 720, "duration_seconds": 6},
    )
    _, body, headers = submission(spec)
    assert body["parameters"] == {"resolution": "720P", "ratio": "16:9", "duration": 6}
    assert headers["X-DashScope-Async"] == "enable"


def first_frame_request(
    settings,
    tmp_path,
    *,
    data=None,
    media_type="image/png",
    protocol="seedance",
    model="doubao-seedance-1-0-pro-250528",
):
    connection, request = native_request(settings, tmp_path, protocol)
    configuration = dict(connection.configuration)
    configuration.pop("configuration_version")
    configuration["model"] = model
    configuration["configuration_version"] = digest(configuration)
    snapshot = deepcopy(request.snapshot)
    snapshot["context"]["provider_configuration"] = configuration
    snapshot["context"]["generation"].update(
        model=model,
        model_config_version=configuration["configuration_version"],
        prompt="角色抬手并转身与另一角色交谈。",
    )
    if data is None:
        data = image_bytes("PNG", (600, 400))
    identity = str(uuid4())
    checksum = hashlib.sha256(data).hexdigest()
    snapshot["inputs"] = [
        {
            "asset_type": "SHOT_IMAGE",
            "source_asset_version_id": identity,
            "checksum": checksum,
            "dependency_type": "REFERENCE_DEPENDENCY",
        }
    ]
    snapshot["context"]["video_images"] = [
        {
            "kind": "CONTROLLED_IMAGE",
            "file_id": str(uuid4()),
            "asset_version_id": identity,
            "checksum": checksum,
            "byte_size": len(data),
            "media_type": media_type,
            "role": "first_frame",
            "metadata_sha256": "0" * 64,
            "scope_sha256": "0" * 64,
        }
    ]
    return MediaConnection(configuration, CANARY), replace(
        request,
        snapshot=snapshot,
        image_inputs=(MediaImageInput("first_frame", media_type, data),),
    )


@pytest.mark.parametrize("kind,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_seedance_first_frame_wire_uses_exact_controlled_bytes(
    settings, tmp_path, kind, media_type
):
    data = image_bytes(kind, (600, 400))
    connection, request = first_frame_request(settings, tmp_path, data=data, media_type=media_type)
    calls = []

    def handler(req):
        calls.append(req)
        assert req.method == "POST"
        assert req.url.path == "/v1/contents/generations/tasks"
        assert req.headers["authorization"] == f"Bearer {CANARY}"
        assert json.loads(req.content) == {
            "model": "doubao-seedance-1-0-pro-250528",
            "content": [
                {"type": "text", "text": "角色抬手并转身与另一角色交谈。"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64," + base64.b64encode(data).decode()
                    },
                    "role": "first_frame",
                },
            ],
            "ratio": "16:9",
            "resolution": "720p",
            "duration": 6,
        }
        return httpx.Response(200, json={"id": "task_1"})

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    assert provider.capabilities.max_reference_images == 1
    receipt = provider.submit(request)
    assert receipt.status == "SUBMITTED" and receipt.request_id == "task_1"
    assert len(calls) == 1
    assert base64.b64encode(data).decode() not in json.dumps(request.snapshot)
    assert "data=" not in repr(request.image_inputs[0])
    assert "data:image/" not in receipt.model_dump_json()


@pytest.mark.parametrize(
    "bad",
    [
        "missing_image",
        "missing_references",
        "missing_source",
        "wrong_version",
        "wrong_checksum",
        "wrong_byte_size",
        "wrong_media_type",
        "unsupported_role",
        "different_bytes",
        "duplicate_reference",
        "second_frame",
        "unapproved_model",
        "different_protocol",
    ],
)
def test_seedance_first_frame_mismatches_are_rejected_before_http(settings, tmp_path, bad):
    options = {}
    if bad == "unapproved_model":
        options["model"] = "doubao-seedance-1-0-pro-unknown"
    elif bad == "different_protocol":
        options = {"protocol": "wan", "model": "wan2.6-i2v"}
    connection, request = first_frame_request(settings, tmp_path, **options)
    references = request.snapshot["context"]["video_images"]
    if bad == "missing_image":
        request = replace(request, image_inputs=())
    elif bad == "missing_references":
        request.snapshot["context"].pop("video_images")
    elif bad == "missing_source":
        request.snapshot["inputs"] = []
    elif bad == "wrong_version":
        references[0]["asset_version_id"] = str(uuid4())
    elif bad == "wrong_checksum":
        references[0]["checksum"] = "f" * 64
    elif bad == "wrong_byte_size":
        references[0]["byte_size"] += 1
    elif bad == "wrong_media_type":
        references[0]["media_type"] = "image/jpeg"
    elif bad == "unsupported_role":
        references[0]["role"] = "last_frame"
        request = replace(
            request, image_inputs=(replace(request.image_inputs[0], role="last_frame"),)
        )
    elif bad == "different_bytes":
        request = replace(
            request,
            image_inputs=(
                replace(request.image_inputs[0], data=image_bytes("PNG", (600, 400), "red")),
            ),
        )
    elif bad == "duplicate_reference":
        references.append(deepcopy(references[0]))
    elif bad == "second_frame":
        another = deepcopy(references[0])
        another["asset_version_id"] = str(uuid4())
        references.append(another)
        source = deepcopy(request.snapshot["inputs"][0])
        source["source_asset_version_id"] = another["asset_version_id"]
        request.snapshot["inputs"].append(source)
        request = replace(request, image_inputs=request.image_inputs * 2)

    def unexpected_http(_):
        pytest.fail("Invalid frozen first-frame input must not reach the provider")

    provider = NativeMediaProvider(
        settings, connection, transport=httpx.MockTransport(unexpected_http)
    )
    with pytest.raises(GatewayFailure) as error:
        provider.submit(request)
    assert error.value.code == "INPUT_INVALID" and error.value.not_accepted


@pytest.mark.parametrize(
    "size,valid",
    [
        ((300, 300), True),
        ((300, 750), True),
        ((750, 300), True),
        ((2000, 2000), True),
        ((299, 400), False),
        ((300, 751), False),
        ((751, 300), False),
        ((4097, 300), False),
        ((2001, 2000), False),
    ],
)
def test_seedance_first_frame_geometry_and_safe_image_boundaries(settings, tmp_path, size, valid):
    _, request = first_frame_request(settings, tmp_path, data=image_bytes("PNG", size))
    spec = GenerationSpec.model_validate(request.snapshot["context"]["generation"])
    if not valid:
        with pytest.raises(DomainError) as error:
            media_submission(spec, request.image_inputs)
        assert error.value.code == "PROVIDER_REFERENCES_UNSUPPORTED"
    else:
        _, body, _ = media_submission(spec, request.image_inputs)
        assert body["content"][1]["role"] == "first_frame"


@pytest.mark.parametrize("bad", ["truncated_png", "truncated_jpeg", "mime_mismatch", "empty"])
def test_seedance_first_frame_invalid_encoding_never_reaches_http(settings, tmp_path, bad):
    if bad == "truncated_jpeg":
        data = image_bytes("JPEG", (600, 400))[:-2]
    elif bad == "empty":
        data = b""
    elif bad == "truncated_png":
        data = image_bytes("PNG", (600, 400))[:-10]
    else:
        data = image_bytes("JPEG", (600, 400))
    connection, request = first_frame_request(settings, tmp_path, data=data)

    def unexpected_http(_):
        pytest.fail("Invalid image encoding must not reach the provider")

    provider = NativeMediaProvider(
        settings, connection, transport=httpx.MockTransport(unexpected_http)
    )
    with pytest.raises(GatewayFailure) as error:
        provider.submit(request)
    assert error.value.code == "INPUT_INVALID" and error.value.not_accepted


def test_seedance_first_frame_byte_and_request_limits_are_strict(settings, tmp_path, monkeypatch):
    _, request = first_frame_request(settings, tmp_path)
    spec = GenerationSpec.model_validate(request.snapshot["context"]["generation"])
    image = request.image_inputs[0]
    monkeypatch.setattr(native_media_protocols, "SEEDANCE_MAX_IMAGE_BYTES", len(image.data))
    with pytest.raises(DomainError, match="30 MiB"):
        media_submission(spec, request.image_inputs)
    monkeypatch.setattr(native_media_protocols, "SEEDANCE_MAX_IMAGE_BYTES", len(image.data) + 1)
    _, body, _ = media_submission(spec, request.image_inputs)
    wire_size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    monkeypatch.setattr(native_media_protocols, "SEEDANCE_MAX_REQUEST_BYTES", wire_size)
    assert media_submission(spec, request.image_inputs)[1] == body
    monkeypatch.setattr(native_media_protocols, "SEEDANCE_MAX_REQUEST_BYTES", wire_size - 1)
    with pytest.raises(DomainError, match="64 MiB"):
        media_submission(spec, request.image_inputs)


def test_seedance_first_frame_declares_and_enforces_twelve_second_maximum(settings, tmp_path):
    connection, request = first_frame_request(settings, tmp_path)
    provider = NativeMediaProvider(settings, connection)
    assert provider.capabilities.max_video_seconds == 12
    spec = GenerationSpec.model_validate(request.snapshot["context"]["generation"])
    spec = spec.model_copy(update={"output_spec": spec.output_spec | {"duration_seconds": 13}})
    with pytest.raises(DomainError) as error:
        media_submission(spec, request.image_inputs)
    assert error.value.code == "PROVIDER_DURATION_UNSUPPORTED"


def test_seedance_first_frame_query_does_not_require_image_bytes(settings, tmp_path):
    connection, request = first_frame_request(settings, tmp_path)
    query_request = replace(request, image_inputs=())
    calls = []

    def handler(req):
        calls.append(req)
        assert req.method == "GET"
        assert req.url.path.endswith("/contents/generations/tasks/task_1")
        assert not req.content
        return httpx.Response(200, json={"status": "running"})

    provider = NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))
    receipt = provider.query(
        ProviderLookup("seedance", connection.configuration["model"], "task_1", query_request)
    )
    assert receipt.status == "RUNNING" and len(calls) == 1
    assert (
        provider.cancel(
            ProviderLookup("seedance", connection.configuration["model"], "task_1", query_request)
        ).status
        == "UNSUPPORTED"
    )
