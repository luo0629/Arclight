"""Known model restrictions apply before voice persistence and dialogue admission."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_audio_tts import create_ok, tts_case
from test_audio_tts_boundaries import boundary_state

from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import DependencyManifest

__all__ = ["tts_case"]


def configure_model(client, settings, model, protocol="openai_tts"):
    settings.service_model_policies = [
        settings.service_model_policies[0].model_copy(update={"model": model, "protocol": protocol})
    ]
    response = client.put(
        "/api/v1/me/model-services/speech",
        json={
            "contract_version": 2,
            "mode": "custom",
            "supplier": "custom",
            "model": model,
            "protocol": protocol,
            "api_key": "synthetic-model-parameter-key",
            "base_url": "https://native.example/v1",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 200, response.text


def save_model_voice(client, case, *, output_spec=None, **parameters):
    return client.post(
        f"/api/v1/projects/{case['pid']}/audio-voices",
        json={
            **case["voice_payload"],
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "parameters": {"voice": "alloy", "speed": 1.1, **parameters},
            "output_spec": output_spec
            if output_spec is not None
            else case["voice_payload"]["output_spec"],
        },
    )


def voice_count(client):
    with client.app.state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(AudioVoiceVersion))


def frozen_parameters(client, row):
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(row["generation_manifest_id"]))
        return manifest.snapshot_json["context"]["generation"]["parameters"]


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_known_tts_model_rejects_voice_instructions_before_persistence_or_budget(
    client, settings, tts_case, model
):
    configure_model(client, settings, model)
    before, count = boundary_state(client), voice_count(client)
    response = save_model_voice(client, tts_case, instructions="平静地说话。")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PROVIDER_PARAMETERS_UNSUPPORTED"
    assert voice_count(client) == count
    assert boundary_state(client) == before
    assert before["reserved"] == ()


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_known_tts_model_rejects_line_instructions_before_task_manifest_or_budget(
    client, settings, tts_case, model
):
    configure_model(client, settings, model)
    response = save_model_voice(client, tts_case)
    assert response.status_code == 201, response.text
    tts_case["payload"]["voice_version_id"] = response.json()["id"]
    before, count = boundary_state(client), voice_count(client)
    response = client.post(
        tts_case["url"],
        json={
            **tts_case["payload"],
            "performance_parameters": {"instructions": "焦急但克制。"},
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PROVIDER_PARAMETERS_UNSUPPORTED"
    assert voice_count(client) == count
    assert boundary_state(client) == before
    assert before["reserved"] == ()


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_known_tts_model_still_admits_frozen_voice_and_line_speed(
    client, settings, tts_case, model
):
    configure_model(client, settings, model)
    response = save_model_voice(client, tts_case, speed=1.25)
    assert response.status_code == 201, response.text
    assert response.json()["parameters"] == {"voice": "alloy", "speed": 1.25}
    tts_case["payload"]["voice_version_id"] = response.json()["id"]
    row, _ = create_ok(client, tts_case, performance_parameters={"speed": 0.8})
    assert frozen_parameters(client, row) == {"voice": "alloy", "speed": 0.8}
    assert len(boundary_state(client)["reserved"]) == 1


@pytest.mark.parametrize("model", ["gpt-4o-mini-tts", "tts-1-compatible"])
def test_supported_and_unknown_compatible_tts_keep_voice_and_line_instructions(
    client, settings, tts_case, model
):
    configure_model(client, settings, model)
    response = save_model_voice(client, tts_case, instructions="温和地说话。")
    assert response.status_code == 201, response.text
    assert response.json()["parameters"]["instructions"] == "温和地说话。"
    tts_case["payload"]["voice_version_id"] = response.json()["id"]
    row, _ = create_ok(client, tts_case, performance_parameters={"instructions": "坚定地说话。"})
    assert frozen_parameters(client, row) == {
        "voice": "alloy",
        "speed": 1.1,
        "instructions": "坚定地说话。",
    }
    assert len(boundary_state(client)["reserved"]) == 1


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("FunAudioLLM/CosyVoice2-0.5B", id="cosyvoice2"),
        pytest.param("fnlp/MOSS-TTSD-v0.5", id="moss-ttsd"),
    ],
)
@pytest.mark.parametrize(
    "output",
    [
        pytest.param({"format": "flac"}, id="flac"),
        pytest.param({"format": "wav", "sample_rate": 48000}, id="wav-48000"),
        pytest.param({"format": "mp3", "sample_rate": 24000}, id="mp3-24000"),
        pytest.param({"format": "opus", "sample_rate": 44100}, id="opus-44100"),
    ],
)
def test_siliconflow_known_models_reject_voice_output_before_persistence_or_budget(
    client, settings, tts_case, model, output
):
    configure_model(client, settings, model, protocol="siliconflow_tts")
    before, count = boundary_state(client), voice_count(client)
    response = save_model_voice(client, tts_case, voice=f"{model}:alex", output_spec=output)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PROVIDER_PARAMETERS_UNSUPPORTED"
    assert voice_count(client) == count
    assert boundary_state(client) == before
    assert before["reserved"] == ()


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("FunAudioLLM/CosyVoice2-0.5B", id="cosyvoice2"),
        pytest.param("fnlp/MOSS-TTSD-v0.5", id="moss-ttsd"),
    ],
)
@pytest.mark.parametrize(
    "output",
    [
        pytest.param({"format": "wav", "sample_rate": 44100}, id="wav-44100"),
        pytest.param({"format": "mp3", "sample_rate": 32000}, id="mp3-32000"),
        pytest.param({"format": "opus", "sample_rate": 48000}, id="opus-48000"),
    ],
)
def test_siliconflow_known_models_admit_valid_frozen_output_to_line_budget(
    client, settings, tts_case, model, output
):
    configure_model(client, settings, model, protocol="siliconflow_tts")
    response = save_model_voice(client, tts_case, voice=f"{model}:alex", output_spec=output)
    assert response.status_code == 201, response.text
    assert response.json()["output_spec"] == output
    tts_case["payload"]["voice_version_id"] = response.json()["id"]
    row, _ = create_ok(client, tts_case)
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(row["generation_manifest_id"]))
        generation = manifest.snapshot_json["context"]["generation"]
        assert generation["output_spec"] == output
        assert generation["model"] == model
    assert len(boundary_state(client)["reserved"]) == 1
