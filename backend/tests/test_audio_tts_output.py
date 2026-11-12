"""Only audio matching the frozen TTS output contract can become a line version."""

import base64
import io
import math
import struct
import wave
import zlib
from uuid import UUID, uuid4

import av
import pytest
from sqlalchemy import func, select
from test_audio_import import compressed_voice
from test_audio_tts import declaration, finish_audio, tts_case
from test_audio_tts_boundaries import boundary_state
from test_native_media_integration import configure

from arclight.application.render_inputs import file_storage
from arclight.media.audio import inspect_audio
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.models import (
    AssetVersion,
    GenerationJob,
    GenerationMediaArtifact,
    StoredFile,
    VisualAsset,
)

__all__ = ["tts_case"]

# Original 100 ms mono sine, 8 kHz, encoded once with libsndfile/Vorbis. Keeping
# the compressed fixture here avoids requiring an external encoder in CI; PyAV's
# available native Vorbis encoder supports only stereo, which would mask a codec bug.
MONO_VORBIS = (
    "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"
)


def wave_data(*, sample_rate=24000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(
            b"".join(
                struct.pack("<h", int(4000 * math.sin(index / 20))) * channels
                for index in range(sample_rate // 10)
            )
        )
    return output.getvalue()


def output_voice(client, settings, case, output_spec):
    configure(client, settings, "speech", "siliconflow_tts", revision=1)
    response = client.post(
        f"/api/v1/projects/{case['pid']}/audio-voices",
        json={
            **case["voice_payload"],
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "output_spec": output_spec,
        },
    )
    assert response.status_code == 201, response.text
    case["voice"] = response.json()
    case["payload"]["voice_version_id"] = case["voice"]["id"]


def assert_rejected_output(client, settings, case, row, data):
    before = boundary_state(client)
    assert before["reserved"]
    endpoint = case["url"] + f"/{row['job_id']}/result"
    response = client.post(endpoint, json=declaration())
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "AUDIO_OUTPUT_MISMATCH"
    assert client.get(endpoint).status_code == 404
    assert boundary_state(client) == before
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, UUID(row["job_id"])).status == "SUCCEEDED"
        artifact = db.scalar(
            select(GenerationMediaArtifact).where(
                GenerationMediaArtifact.job_id == UUID(row["job_id"])
            )
        )
        assert artifact.error_code is None and artifact.file_id is not None
        raw = db.get(StoredFile, artifact.file_id)
        assert raw.asset_version_id is None and not raw.rights_confirmed
        assert file_storage(settings, raw).read(raw.object_key, len(data), raw.checksum) == data
        asset = db.get(VisualAsset, UUID(row["target_asset_id"]))
        assert asset.revision == 0 and asset.current_version_id is None
        assert not db.scalar(select(AssetVersion.id).where(AssetVersion.asset_id == asset.id))
        assert (
            db.scalar(
                select(func.count())
                .select_from(AudioLineBinding)
                .where(AudioLineBinding.snapshot_id == UUID(case["snapshot"]["id"]))
            )
            == 0
        )


@pytest.mark.parametrize(
    "output_spec,sample_rate,channels",
    [
        pytest.param({"format": "mp3"}, 24000, 1, id="wrong-format"),
        pytest.param({"format": "wav"}, 24000, 2, id="wrong-default-channel-count"),
        pytest.param(
            {"format": "wav", "channels": 1, "sample_rate": 24000},
            16000,
            1,
            id="wrong-explicit-sample-rate",
        ),
    ],
)
def test_tts_rejects_real_output_mismatch_without_losing_result_or_budget(
    client, settings, tts_case, output_spec, sample_rate, channels
):
    output_voice(client, settings, tts_case, output_spec)
    data = wave_data(sample_rate=sample_rate, channels=channels)
    row, _, _ = finish_audio(client, settings, tts_case, data=data)
    assert_rejected_output(client, settings, tts_case, row, data)


@pytest.mark.parametrize("format_name", ["wav", "mp3", "opus", "flac"])
def test_tts_accepts_real_audio_matching_explicit_frozen_output(
    client, settings, tts_case, format_name
):
    rate = 48000 if format_name in {"mp3", "opus"} else 24000
    output_voice(
        client,
        settings,
        tts_case,
        {"format": format_name, "channels": 1, "sample_rate": rate},
    )
    if format_name in {"mp3", "opus"}:
        data = compressed_voice("audio/mpeg" if format_name == "mp3" else "audio/ogg")
    elif format_name == "flac":
        output = io.BytesIO()
        with (
            av.open(io.BytesIO(wave_data())) as source,
            av.open(output, "w", format="flac") as target,
        ):
            stream = target.add_stream("flac", rate=rate)
            stream.layout = "mono"
            for frame in source.decode(audio=0):
                for packet in stream.encode(frame):
                    target.mux(packet)
            for packet in stream.encode(None):
                target.mux(packet)
        data = output.getvalue()
    else:
        data = wave_data()
    row, _, _ = finish_audio(client, settings, tts_case, data=data)
    response = client.post(tts_case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == 201, response.text
    validation = response.json()["validation"]
    assert validation["sample_rate"] == rate and validation["channels"] == 1
    assert (
        validation["media_type"]
        == {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "opus": "audio/ogg",
            "flac": "audio/flac",
        }[format_name]
    )
    if format_name == "opus":
        assert validation["codec"] == "opus"
    assert validation["duration_ms"] > 0 and not validation["silence"]["silent"]


def test_tts_rejects_real_mono_vorbis_for_opus_despite_matching_ogg_container(
    client, settings, tts_case
):
    output_voice(client, settings, tts_case, {"format": "opus", "channels": 1})
    data = zlib.decompress(base64.b64decode(MONO_VORBIS))
    validation = inspect_audio(data, "audio/ogg")
    assert validation["codec"] == "vorbis" and validation["channels"] == 1
    assert validation["sample_rate"] == 8000 and validation["duration_ms"] > 0
    assert not validation["silence"]["silent"]
    row, _, _ = finish_audio(client, settings, tts_case, data=data)
    assert_rejected_output(client, settings, tts_case, row, data)
