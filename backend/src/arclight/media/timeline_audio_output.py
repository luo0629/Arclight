"""Verify final AAC peaks and, if necessary, re-encode audio while copying video packets."""

import sys
from fractions import Fraction
from io import BytesIO
from math import ceil, isfinite

from arclight.media.render_plan import AUDIO_RATE, RenderPlan
from arclight.media.timeline_mixer import AudioMixer

MAX_AAC_PASSES = 3
OUTPUT_PEAK = 0.98


def audio_peak(movie: bytes, expected_samples: int) -> float:
    import av

    peak, samples = 0.0, 0
    with av.open(BytesIO(movie), format="mp4", options={"protocol_whitelist": "pipe"}) as source:
        if not isinstance(source, av.container.InputContainer):
            raise ValueError("Input container required")
        stream = source.streams.audio[0]
        stream.codec_context.thread_count = 1
        for frame in source.decode(stream):
            if frame.format.name != "fltp" or len(frame.layout.channels) != 2:
                raise ValueError("Unexpected encoded audio format")
            samples += frame.samples
            if samples > expected_samples + 2048:
                raise ValueError("Encoded audio exceeds sample workload")
            for plane in frame.planes:
                for value in memoryview(plane).cast("f")[: frame.samples]:
                    if not isfinite(value):
                        raise ValueError("Non-finite encoded audio samples")
                    peak = max(peak, abs(value))
    if samples < expected_samples:
        raise ValueError("Encoded audio has insufficient samples")
    return peak


def remux_with_audio(movie: bytes, plan: RenderPlan, gain: float) -> bytes:
    import av

    output = BytesIO()
    with (
        av.open(BytesIO(movie), format="mp4", options={"protocol_whitelist": "pipe"}) as source,
        av.open(output, mode="w", format="mp4") as container,
        AudioMixer(plan, output_gain=gain) as mixer,
    ):
        if not isinstance(source, av.container.InputContainer):
            raise ValueError("Input container required")
        video = container.add_stream_from_template(source.streams.video[0])
        audio = container.add_stream("aac", rate=AUDIO_RATE)
        audio.layout = "stereo"
        audio.bit_rate = plan.output.audio_bitrate
        audio.codec_context.thread_count = 1
        total = (plan.frames * AUDIO_RATE + plan.output.fps - 1) // plan.output.fps
        sample = 0

        def advance(target: int) -> None:
            nonlocal sample
            while sample < target:
                count = min(1024, target - sample)
                for encoded in audio.encode(mixer.frame(sample, count)):
                    container.mux(encoded)
                sample += count

        for packet in source.demux(video=0):
            if packet.dts is None:
                continue
            assert packet.pts is not None and packet.time_base is not None
            advance(
                min(
                    total,
                    ceil(
                        (packet.pts * packet.time_base + Fraction(1, plan.output.fps)) * AUDIO_RATE
                    ),
                )
            )
            packet.stream = video
            container.mux(packet)
        advance(total)
        for packet in audio.encode(None):
            container.mux(packet)
    return output.getvalue()


def checked_audio_output(movie: bytes, plan: RenderPlan) -> bytes:
    if not plan.audio_clips:
        return movie
    samples = (plan.frames * AUDIO_RATE + plan.output.fps - 1) // plan.output.fps
    gain = 1.0
    for attempt in range(MAX_AAC_PASSES):
        peak = audio_peak(movie, samples)
        if peak <= OUTPUT_PEAK:
            return movie
        if attempt + 1 < MAX_AAC_PASSES:
            # Based on actual encoded peaks, independent of track count. Re-mix original PCM.
            gain *= min(0.8, 0.8 / peak)
            movie = remux_with_audio(movie, plan, gain)
            sys.stderr.write(f"ARCLIGHT_PROGRESS:{96 + attempt}\n")
            sys.stderr.flush()
    raise ValueError("AAC output exceeds peak limit after bounded correction")
