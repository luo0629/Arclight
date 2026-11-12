"""Use real decoded samples to check the additive audio inspection report."""

import math
import struct
from array import array
from io import BytesIO

import av
import pytest

from arclight.media.audio import inspect_audio
from arclight.media.validation import validate_file


def pcm_wave(values, encoding="s16", channels=1, rate=8000):
    bits = {"u8": 8, "s16": 16, "s24": 24, "s32": 32, "f32": 32}[encoding]
    if encoding == "f32":
        payload = struct.pack(f"<{len(values)}f", *values)
    elif encoding == "u8":
        payload = bytes(round(value * 128) + 128 for value in values)
    else:
        width = bits // 8
        payload = b"".join(
            round(value * (1 << (bits - 1))).to_bytes(width, "little", signed=True)
            for value in values
        )
    fmt = struct.pack(
        "<HHIIHH",
        3 if encoding == "f32" else 1,
        channels,
        rate,
        rate * channels * bits // 8,
        channels * bits // 8,
        bits,
    )
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(payload)) + payload
    chunks += bytes(len(payload) % 2)
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def compressed_samples(media_type, *, silent=False):
    codec, format_name = {
        "audio/mpeg": ("libmp3lame", "mp3"),
        "audio/ogg": ("libopus", "ogg"),
        "audio/flac": ("flac", "flac"),
    }[media_type]
    output = BytesIO()
    with av.open(output, "w", format=format_name) as container:
        stream = container.add_stream(codec, rate=48000)
        stream.layout = "stereo"
        samples = 4800
        values = array("h")
        for index in range(samples):
            value = 0 if silent else round(12000 * math.sin(index * math.tau * 440 / 48000))
            values.extend((value, -value))
        frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
        frame.planes[0].update(values.tobytes())
        frame.sample_rate = 48000
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


@pytest.mark.parametrize("encoding", ["u8", "s16", "s24", "s32", "f32"])
@pytest.mark.parametrize("amplitude", [0, 0.5])
def test_pcm_formats_have_consistent_normalized_report(encoding, amplitude):
    result = inspect_audio(pcm_wave([amplitude] * 800, encoding), "audio/wav")
    report = result["silence"]
    assert result["duration_ms"] == 100
    assert result["sample_rate"] == 8000
    assert result["channels"] == 1
    assert (
        result["codec"]
        == {
            "u8": "pcm_u8",
            "s16": "pcm_s16le",
            "s24": "pcm_s24le",
            "s32": "pcm_s32le",
            "f32": "pcm_f32le",
        }[encoding]
    )
    assert report["schema_version"] == "audio-silence/1"
    assert report["threshold_dbfs"] == -60
    assert report["threshold_pcm16"] == 32
    assert report["silent"] is (amplitude == 0)
    assert report["peak"] == pytest.approx(amplitude, abs=1 / 32768)
    assert report["rms"] == pytest.approx(amplitude, abs=1 / 32768)
    assert report["silent_sample_fraction"] == (1 if amplitude == 0 else 0)


@pytest.mark.parametrize("encoding", ["s16", "s24", "s32", "f32"])
@pytest.mark.parametrize("sample", [-33, -32, 0, 32, 33])
def test_near_silence_uses_documented_absolute_threshold(encoding, sample):
    report = inspect_audio(pcm_wave([sample / 32768] * 80, encoding), "audio/wav")["silence"]
    assert report["silent"] is (abs(sample) <= 32)
    assert report["peak"] == pytest.approx(abs(sample) / 32768, abs=1 / 32768)
    assert report["silent_sample_fraction"] == (1 if abs(sample) <= 32 else 0)


@pytest.mark.parametrize("right", [0, -0.5])
def test_stereo_channels_cannot_cancel_or_hide_active_channel(right):
    report = inspect_audio(pcm_wave([0.5, right] * 800, channels=2), "audio/wav")["silence"]
    assert report["silent"] is False
    assert report["peak"] == 0.5
    assert report["rms"] == pytest.approx(0.5 if right else math.sqrt(0.125))
    assert report["silent_sample_fraction"] == (0 if right else 0.5)


def test_full_file_inspection_includes_late_signal_and_exact_samples():
    values = [0] * 7999 + [0.5]
    result = inspect_audio(pcm_wave(values), "audio/wav")
    assert result["duration_ms"] == 1000
    assert result["silence"]["silent"] is False
    assert result["silence"]["peak"] == 0.5
    assert result["silence"]["rms"] == pytest.approx(0.5 / math.sqrt(8000))
    assert result["silence"]["silent_sample_fraction"] == 7999 / 8000


@pytest.mark.parametrize("media_type", ["audio/mpeg", "audio/ogg", "audio/flac"])
@pytest.mark.parametrize("silent", [False, True])
def test_compressed_audio_reports_real_decoded_waveform(media_type, silent):
    result = inspect_audio(compressed_samples(media_type, silent=silent), media_type)
    assert result["sample_rate"] == 48000
    assert result["channels"] == 2
    assert 90 <= result["duration_ms"] <= 160
    assert result["codec"] in {"mp3", "mp3float", "opus", "flac"}
    report = result["silence"]
    assert report["silent"] is silent
    if silent:
        assert report["peak"] <= 32 / 32768
        assert report["silent_sample_fraction"] == 1
    else:
        assert 0.1 < report["peak"] < 1
        assert 0.05 < report["rms"] < 0.6
        assert report["silent_sample_fraction"] < 0.2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_float_samples_are_rejected(value):
    with pytest.raises(ValueError, match="Non-finite"):
        inspect_audio(pcm_wave([0] * 79 + [value], "f32"), "audio/wav")


def test_generic_upload_still_accepts_and_reports_silence():
    result = validate_file(pcm_wave([0] * 80), "AUDIO", "silence.wav", "audio/wav")
    assert result.details["silence"]["silent"] is True
    assert result.details["duration_ms"] == 10
