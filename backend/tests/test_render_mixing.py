"""Real PCM and MP4 observations for the sealed renderer's audio contract."""

import base64
import math
import wave
from array import array
from io import BytesIO

import av
import pytest
from PIL import Image
from pydantic import ValidationError
from test_render_encoder import clip, profile

from arclight.domain.timeline_audio import ClipAudio
from arclight.media.execution import execute_media
from arclight.media.render_plan import RenderAudioClip, RenderAudioSource, RenderPlan
from arclight.media.timeline_encoder import encode_timeline
from arclight.media.timeline_mixer import AudioMixer


def tone(rate=48000, channels=1, duration_ms=1000, amplitude=0.2, frequency=1000):
    values = array("h")
    for index in range(rate * duration_ms // 1000):
        value = round(32767 * amplitude * math.sin(2 * math.pi * frequency * index / rate))
        values.append(value)
        if channels == 2:
            values.append(-value // 2)
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(values.tobytes())
    return RenderAudioSource(
        data=base64.b64encode(output.getvalue()).decode(), media_type="audio/wav"
    )


def sound(source_index=0, start_ms=0, duration_ms=1000, **changes):
    return RenderAudioClip(
        source_index=source_index, start_ms=start_ms, duration_ms=duration_ms, **changes
    )


def audio_plan(sources, sounds, duration=1000, **changes):
    return RenderPlan(
        output=profile(fps=25),
        duration_ms=duration,
        clips=(clip(Image.new("RGB", (16, 16), "black"), duration_ms=duration),),
        audio_sources=tuple(sources),
        audio_clips=tuple(sounds),
        **changes,
    )


def mixed_samples(plan, chunk=1001):
    left, right = [], []
    with AudioMixer(plan) as mixer:
        for start in range(0, plan.duration_ms * 48, chunk):
            frame = mixer.frame(start, min(chunk, plan.duration_ms * 48 - start))
            left.extend(memoryview(frame.planes[0]).cast("f")[: frame.samples])
            right.extend(memoryview(frame.planes[1]).cast("f")[: frame.samples])
    return left, right


def rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


def decoded_audio(movie):
    channels = ([], [])
    with av.open(BytesIO(movie)) as container:
        audio = container.streams.audio[0]
        assert audio.sample_rate == 48000 and audio.layout.name == "stereo"
        for frame in container.decode(audio=0):
            for channel, plane in zip(channels, frame.planes, strict=True):
                channel.extend(memoryview(plane).cast("f")[: frame.samples])
    return channels


@pytest.mark.parametrize("rate,channels", [(8000, 1), (16000, 1), (44100, 2), (48000, 2)])
def test_actual_rate_conversion_preserves_mono_level_and_stereo_phase(rate, channels):
    plan = audio_plan([tone(rate, channels)], [sound()])
    left, right = mixed_samples(plan)
    assert len(left) == len(right) == 48000
    assert rms(left[4800:43200]) == pytest.approx(0.2 / math.sqrt(2), abs=0.002)
    scale = 1 if channels == 1 else -0.5
    assert max(abs(rv - lv * scale) for lv, rv in zip(left, right, strict=True)) < 0.0001


@pytest.mark.parametrize(
    "codec,container_format,media_type",
    [
        ("libmp3lame", "mp3", "audio/mpeg"),
        ("libopus", "ogg", "audio/ogg"),
        ("flac", "flac", "audio/flac"),
    ],
)
def test_supported_compressed_sources_are_decoded_trimmed_and_mixed(
    codec, container_format, media_type
):
    original = tone(amplitude=0.2)
    source_bytes = base64.b64decode(original.data)
    output = BytesIO()
    with (
        av.open(BytesIO(source_bytes), format="wav") as source,
        av.open(output, "w", format=container_format) as container,
    ):
        stream = container.add_stream(codec, rate=48000)
        stream.layout = "mono"
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    compressed = RenderAudioSource(
        data=base64.b64encode(output.getvalue()).decode(), media_type=media_type
    )
    left, right = mixed_samples(
        audio_plan([compressed], [sound(duration_ms=800, source_start_ms=100)], duration=800)
    )
    assert len(left) == len(right) == 38400
    assert rms(left[4800:30000]) == pytest.approx(0.2 / math.sqrt(2), abs=0.02)
    assert left == right


def test_nonfinite_float_audio_is_rejected_in_actual_decode():
    values = array("f", [float("nan")] * 4800)
    frame = av.AudioFrame(format="flt", layout="mono", samples=4800)
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = 48000
    output = BytesIO()
    with av.open(output, "w", format="wav") as container:
        stream = container.add_stream("pcm_f32le", rate=48000)
        stream.layout = "mono"
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    bad = RenderAudioSource(
        data=base64.b64encode(output.getvalue()).decode(), media_type="audio/wav"
    )
    with pytest.raises(ValueError, match="Non-finite"):
        mixed_samples(audio_plan([bad], [sound(duration_ms=100)], duration=100))


def test_source_in_point_and_overlaps_are_sample_exact_with_silent_gaps():
    plan = audio_plan(
        [tone(frequency=440), tone(channels=2, amplitude=0.1, frequency=660)],
        [
            sound(start_ms=200, duration_ms=500, source_start_ms=137),
            sound(1, 400, 300, source_start_ms=53),
        ],
    )
    left, right = mixed_samples(plan)
    assert not any(left[:9600]) and not any(left[33600:])
    for sample in (9600, 11003, 19200, 23011, 30012, 33599):
        voice = 0.2 * math.sin(2 * math.pi * 440 * (sample - 9600 + 6576) / 48000)
        music = (
            0.1 * math.sin(2 * math.pi * 660 * (sample - 19200 + 2544) / 48000)
            if sample >= 19200
            else 0
        )
        assert left[sample] == pytest.approx(voice + music, abs=0.0001)
        assert right[sample] == pytest.approx(voice - music / 2, abs=0.0001)


def test_three_tracks_add_at_unity_and_fixed_peak_ceiling_does_not_normalize_by_track_count():
    unity = audio_plan([tone(amplitude=0.1)], [sound(), sound(), sound()])
    left, _ = mixed_samples(unity)
    assert left[12] == pytest.approx(0.3, abs=0.0002)
    loud = audio_plan([tone(amplitude=0.8)], [sound(), sound(), sound()])
    left, right = mixed_samples(loud)
    assert max(left) == pytest.approx(0.95) and min(left) == pytest.approx(-0.95)
    assert left == right and all(math.isfinite(value) for value in left)


def test_gain_envelope_and_both_fades_multiply_at_actual_sample_positions():
    automation = ClipAudio.model_validate(
        {
            "purpose_role": "BACKGROUND",
            "gain_milli": 500,
            "fade_in_ms": 200,
            "fade_out_ms": 200,
            "envelope": [
                {"offset_ms": 0, "gain_milli": 1000},
                {"offset_ms": 500, "gain_milli": 500},
                {"offset_ms": 1000, "gain_milli": 1000},
            ],
        }
    )
    left, _ = mixed_samples(audio_plan([tone()], [sound(audio=automation)]))
    for sample in (12, 4812, 12012, 24012, 36012, 43212, 47988):
        ms = sample / 48
        envelope = 1 - ms / 1000 if ms < 500 else ms / 1000
        gain = 0.5 * envelope * min(1, ms / 200) * min(1, (1000 - ms) / 200)
        expected = 0.2 * math.sin(2 * math.pi * 1000 * sample / 48000) * gain
        assert left[sample] == pytest.approx(expected, abs=0.0001)


@pytest.mark.parametrize("duration,start", [(1001, 0), (900, 101)])
def test_actual_sample_shortfall_fails_instead_of_padding_source(duration, start):
    plan = audio_plan([tone()], [sound(duration_ms=duration, source_start_ms=start)], duration=1100)
    with pytest.raises(ValueError, match="insufficient decoded samples"):
        mixed_samples(plan)


@pytest.mark.parametrize("payload", ["https://example.invalid/tone.wav", "C:/private.wav", "!!!!"])
def test_child_rejects_audio_locations_and_invalid_binary(payload):
    source = RenderAudioSource(data=payload, media_type="audio/wav")
    with pytest.raises((ValueError, av.error.FFmpegError)):
        encode_timeline(audio_plan([source], [sound()]).model_dump_json().encode())


def test_sealed_mp4_has_actual_stereo_audio_and_preserves_silent_windows():
    plan = audio_plan([tone(channels=2, amplitude=0.3)], [sound(start_ms=200, duration_ms=600)])
    progress = []
    movie = execute_media(
        "render", "timeline", plan.model_dump_json().encode(), observer=progress.append
    )
    left, right = decoded_audio(movie)
    assert rms(left[2000:7000]) < 0.001 and rms(left[42000:47000]) < 0.001
    assert rms(left[14000:30000]) == pytest.approx(0.3 / math.sqrt(2), abs=0.015)
    assert rms(right[14000:30000]) == pytest.approx(0.15 / math.sqrt(2), abs=0.015)
    assert sum(lv * rv for lv, rv in zip(left[14000:30000], right[14000:30000], strict=True)) < 0
    assert [value for value in progress if value is not None][-1] == 100


def test_actual_aac_overlap_remains_finite_with_fixed_peak_headroom():
    plan = audio_plan([tone(amplitude=0.8)], [sound(), sound(), sound()])
    movie = execute_media("render", "timeline", plan.model_dump_json().encode())
    left, right = decoded_audio(movie)
    assert all(math.isfinite(value) for channel in (left, right) for value in channel)
    assert max(map(abs, left)) <= 0.98001
    assert 0.2 < rms(left[5000:40000]) < 0.7


def test_peak_correction_preserves_exact_video_packets_and_stereo_relative_levels(monkeypatch):
    from arclight.media import timeline_audio_output, timeline_encoder

    original_movies = []
    correction = timeline_audio_output.checked_audio_output

    def observe(movie, plan):
        original_movies.append(movie)
        return correction(movie, plan)

    monkeypatch.setattr(timeline_encoder, "checked_audio_output", observe)
    plan = audio_plan([tone(channels=2, amplitude=0.8)], [sound(), sound(), sound()])
    pcm_left, pcm_right = mixed_samples(plan)
    assert (
        max(abs(right + left / 2) for left, right in zip(pcm_left, pcm_right, strict=True)) < 0.0001
    )
    movie = timeline_encoder.encode_timeline(plan.model_dump_json().encode())
    assert movie != original_movies[0]

    def packets(data):
        with av.open(BytesIO(data), format="mp4") as source:
            return [
                (bytes(packet), packet.pts * packet.time_base)
                for packet in source.demux(video=0)
                if packet.pts is not None
            ]

    assert packets(movie) == packets(original_movies[0])
    assert max(map(abs, decoded_audio(movie)[0])) <= 0.98001


def test_encoded_peak_correction_exhaustion_fails_after_bounded_attempts(monkeypatch):
    from arclight.media import timeline_audio_output

    gains = []
    monkeypatch.setattr(timeline_audio_output, "audio_peak", lambda *_: 2.0)

    def remux(movie, plan, gain):
        gains.append(gain)
        return movie

    monkeypatch.setattr(timeline_audio_output, "remux_with_audio", remux)
    plan = audio_plan([tone()], [sound()])
    with pytest.raises(ValueError, match="after bounded correction"):
        timeline_audio_output.checked_audio_output(b"unpublishable", plan)
    assert len(gains) == 2 and 0 < gains[1] < gains[0] < 1


def test_plan_bounds_decoder_concurrency_and_decoded_prefix_work_without_byte_cap():
    with pytest.raises(ValidationError, match="active audio decoder"):
        audio_plan([tone()], [sound() for _ in range(17)])
    with pytest.raises(ValidationError, match="decoded audio workload"):
        audio_plan(
            [tone()],
            [sound(start_ms=i * 1000, duration_ms=1000, source_start_ms=599000) for i in range(7)],
            duration=7000,
        )
    # Sequential clips and touching boundaries release their decoders, including sub-block cuts.
    short = audio_plan(
        [tone()], [sound(start_ms=i, duration_ms=1) for i in range(100)], duration=100
    )
    assert len(mixed_samples(short)[0]) == 4800


@pytest.mark.parametrize(
    "change",
    [
        {"source_index": 1},
        {"start_ms": 1},
        {"duration_ms": 600001},
        {"source_start_ms": 599001},
        {"path": "private.wav"},
        {"gain": "volume=9"},
    ],
)
def test_plan_rejects_unbound_sources_ranges_and_untrusted_audio_fields(change):
    body = audio_plan([tone()], [sound()]).model_dump(mode="json")
    body["audio_clips"][0].update(change)
    with pytest.raises(ValidationError):
        RenderPlan.model_validate(body)
