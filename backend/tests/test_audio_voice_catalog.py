"""Voice directory scope, exact-route changes and bounded account lookup."""

from uuid import uuid4

import httpx
from speech_catalog_support import catalog_response
from test_model_services import account
from test_native_media_integration import configure, project


def save_speech(client, supplier, protocol, model, *, revision=0, base_url=""):
    response = client.put(
        "/api/v1/me/model-services/speech",
        json={
            "supplier": supplier,
            "protocol": protocol,
            "model": model,
            "base_url": base_url,
            "api_key": "catalog-synthetic-key",
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text


def directory(client, project_id):
    response = client.get(f"/api/v1/projects/{project_id}/audio-voice-catalog")
    assert response.status_code == 200, response.text
    return response.json()


def save_voice(client, project_id, version, voice, *, expected_version_no=0):
    return client.post(
        f"/api/v1/projects/{project_id}/audio-voices",
        json={
            "creation_key": str(uuid4()),
            "speaker": "旁白",
            "expected_version_no": expected_version_no,
            "model_config_version": version,
            "parameters": {"voice": voice},
        },
    )


def test_published_catalog_and_new_voice_are_bound_to_the_exact_model(client, settings):
    account(client)
    project_id = project(client)
    assert directory(client, project_id)["status"] == "NOT_CONFIGURED"
    configure(client, settings, "image")
    save_speech(client, "openai", "openai_tts", "tts-1")
    first = directory(client, project_id)
    assert first["status"] == "READY" and first["supplier"] == "openai"
    assert first["model"] == "tts-1"
    assert len(first["options"]) == 9
    assert "alloy" in {option["id"] for option in first["options"]}
    assert "instructions" not in first["supported_parameters"]
    assert "catalog-synthetic-key" not in str(first)

    disallowed = save_voice(client, project_id, first["configuration_version"], "marin")
    assert disallowed.status_code == 422
    assert disallowed.json()["error"]["code"] == "AUDIO_VOICE_NOT_IN_CATALOG"

    save_speech(client, "openai", "openai_tts", "gpt-4o-mini-tts", revision=1)
    current = directory(client, project_id)
    assert current["configuration_version"] != first["configuration_version"]
    assert "marin" in {option["id"] for option in current["options"]}
    assert "instructions" in current["supported_parameters"]
    stale = save_voice(client, project_id, first["configuration_version"], "alloy")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "MEDIA_SERVICE_CONFIG_CHANGED"
    saved = save_voice(client, project_id, current["configuration_version"], "marin")
    assert saved.status_code == 201, saved.text

    save_speech(client, "openai", "openai_tts", "unknown-model", revision=2)
    unavailable = directory(client, project_id)
    assert unavailable["status"] == "UNAVAILABLE" and unavailable["options"] == []
    rejected = save_voice(
        client, project_id, unavailable["configuration_version"], "alloy", expected_version_no=1
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "AUDIO_VOICE_CATALOG_UNAVAILABLE"
    history = client.get(f"/api/v1/projects/{project_id}/audio-voices").json()
    assert history == [saved.json()]

    account(client)
    assert client.get(f"/api/v1/projects/{project_id}/audio-voice-catalog").status_code == 404


def test_dynamic_catalog_filters_model_and_sanitizes_query_failure(client, settings, monkeypatch):
    account(client)
    project_id = project(client)
    configure(client, settings, "image")
    model = "FunAudioLLM/CosyVoice2-0.5B"
    save_speech(client, "siliconflow", "siliconflow_tts", model)
    available = True
    requests = []

    def respond(request):
        if request.url.host == "docs.siliconflow.cn":
            return catalog_response(request)
        assert request.method == "GET"
        assert str(request.url) == "https://api.siliconflow.cn/v1/audio/voice/list"
        assert request.headers["Authorization"] == "Bearer catalog-synthetic-key"
        requests.append(request.method)
        if not available:
            return httpx.Response(401, json={"error": {"message": "private supplier details"}})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "model": model,
                        "uri": "speech:test:demo",
                        "customName": "叙述",
                        "text": "private voice sample text",
                    },
                    {
                        "model": model,
                        "uri": "speech:test:demo",
                        "customName": "叙述",
                    },
                    {
                        "model": "fnlp/MOSS-TTSD-v0.5",
                        "uri": "speech:test:other",
                        "customName": "其他模型",
                    },
                ]
            },
        )

    monkeypatch.setattr(
        "arclight.providers.speech_catalogs.PublicHTTPTransport",
        lambda _timeout, **_kwargs: httpx.MockTransport(respond),
    )
    ready = directory(client, project_id)
    assert ready["status"] == "READY"
    assert [option["id"] for option in ready["options"]].count("speech:test:demo") == 1
    assert "speech:test:other" not in str(ready)
    assert "private voice sample text" not in str(ready)
    assert f"{model}:alex" in {option["id"] for option in ready["options"]}
    version = ready["configuration_version"]
    saved = save_voice(client, project_id, version, "speech:test:demo")
    assert saved.status_code == 201, saved.text
    assert requests == ["GET", "GET"]

    available = False
    failed = directory(client, project_id)
    assert failed["status"] == "QUERY_FAILED" and failed["options"] == []
    assert failed["error_code"] == "PROVIDER_AUTHENTICATION_FAILED"
    assert "private supplier details" not in str(failed)
    rejected = save_voice(client, project_id, version, "speech:test:demo", expected_version_no=1)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "AUDIO_VOICE_CATALOG_QUERY_FAILED"


def test_new_custom_model_discovers_only_its_published_voices(client, settings, monkeypatch):
    account(client)
    project_id = project(client)
    configure(client, settings, "image")
    base = "https://voices.example/gateway/v1"
    model = "model-maker/brand-new-model:v2"
    save_speech(client, "custom", "openai_tts", model, base_url=base)
    response_status = 200
    result_id = "new-upstream-voice"

    def respond(request):
        assert request.method == "GET"
        assert str(request.url.copy_with(query=None)) == f"{base}/audio/voice-catalog"
        assert dict(request.url.params) == {"model": model}
        assert request.headers["Authorization"] == "Bearer catalog-synthetic-key"
        if response_status != 200:
            return httpx.Response(response_status)
        return httpx.Response(
            200,
            json={
                "models": {
                    "unrelated": [{"id": "other-voice", "label": "Another model"}],
                    model: [
                        {
                            "id": result_id,
                            "label": "First voice",
                            "description": "Warm narration",
                        },
                        {"id": result_id, "label": "Duplicate"},
                    ],
                }
            },
        )

    monkeypatch.setattr(
        "arclight.providers.speech_catalogs.PublicHTTPTransport",
        lambda _timeout, **_kwargs: httpx.MockTransport(respond),
    )
    found = directory(client, project_id)
    assert found["status"] == "READY"
    assert found["options"] == [
        {"id": result_id, "label": "First voice", "description": "Warm narration"}
    ]
    version = found["configuration_version"]
    result = save_voice(client, project_id, version, "other-voice")
    assert result.status_code == 422 and result.json()["error"]["code"] == (
        "AUDIO_VOICE_NOT_IN_CATALOG"
    )
    assert save_voice(client, project_id, version, result_id).status_code == 201

    result_id = "second-upstream-voice"
    assert directory(client, project_id)["options"][0]["id"] == result_id

    response_status = 404
    assert directory(client, project_id)["status"] == "UNAVAILABLE"
    response_status = 503
    assert directory(client, project_id)["status"] == "QUERY_FAILED"


def test_workspace_discovers_new_markdown_model_and_applies_its_regional_scope(
    client, settings, monkeypatch
):
    account(client)
    project_id = project(client)
    configure(client, settings, "image")
    model = "future-speech-snapshot"
    document = """## 系统音色

### other-model
<tr><td>**名称**: 其他模型</td>
<td>**voice 参数**: <code>other_voice</code></td></tr>

### future-speech-snapshot
<Note>
  跨地域音色支持北京和新加坡地域，其他类型音色仅支持北京地域。
</Note>
<Tabs>
  <Tab title="跨地域音色">
    <tr><td>**音色名称**: 晨星</td>
    <td>**voice 参数**: <code>two\_regions</code></td>
    <td><Tabs><Tab title="试听"><source src="https://preview.example/sound.wav" /></Tab></Tabs>
    </td></tr>
  </Tab>
  <Tab title="北京音色">
    <tr><td>**名称**: 青芒<p>**voice 参数**: \x60beijing_only\x60</p>
    <p>**描述**: 轻松旁白</p></td></tr>
  </Tab>
</Tabs>
"""
    requested = []
    now = 301.0
    monkeypatch.setattr("arclight.providers.speech_catalogs.monotonic", lambda: now)

    def respond(request):
        assert request.method == "GET"
        assert (request.url.host, request.url.path) == (
            "help.aliyun.com",
            "/zh/model-studio/qwen-audio-tts-voice-list.md",
        )
        assert "Authorization" not in request.headers
        requested.append(request.url.host)
        return httpx.Response(200, text=document)

    monkeypatch.setattr(
        "arclight.providers.speech_catalogs.PublicHTTPTransport",
        lambda _timeout, **_kwargs: httpx.MockTransport(respond),
    )
    for revision, region, ids in [
        (0, "cn-beijing", {"two_regions", "beijing_only"}),
        (1, "ap-southeast-1", {"two_regions"}),
    ]:
        save_speech(
            client,
            "custom",
            "qwen_audio_tts",
            model,
            revision=revision,
            base_url=f"https://workspace-123.{region}.maas.aliyuncs.com/api/v1",
        )
        result = directory(client, project_id)
        assert result["status"] == "READY"
        assert {voice["id"] for voice in result["options"]} == ids
        assert "preview.example" not in str(result)
    assert requested == ["help.aliyun.com"]
    document = document.replace(r"two\_regions", "new_upstream_voice")
    cached = directory(client, project_id)
    assert [voice["id"] for voice in cached["options"]] == ["two_regions"]
    assert len(requested) == 1

    now += 300
    refreshed = directory(client, project_id)
    assert [voice["id"] for voice in refreshed["options"]] == ["new_upstream_voice"]
    assert requested == ["help.aliyun.com", "help.aliyun.com"]
