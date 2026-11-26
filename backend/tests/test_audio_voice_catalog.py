"""Voice directory scope, exact-route changes and bounded account lookup."""

from uuid import uuid4

import httpx
from test_model_services import account
from test_native_media_integration import configure, project


def save_speech(client, supplier, protocol, model, *, revision=0):
    response = client.put(
        "/api/v1/me/model-services/speech",
        json={
            "supplier": supplier,
            "protocol": protocol,
            "model": model,
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


def test_builtin_catalog_and_new_voice_are_bound_to_the_exact_model(client, settings):
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
        lambda _: httpx.MockTransport(respond),
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
