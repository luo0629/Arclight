"""Streaming, sample-clocked audio mixing inside the sealed media process."""

from __future__ import annotations

import base64
from array import array
from bisect import bisect_right
from collections.abc import Generator
from fractions import Fraction
from io import BytesIO
from math import isfinite
from typing import TYPE_CHECKING

from arclight.media.audio import (
    AUDIO_CODECS,
    AUDIO_FORMATS,
    MAX_AUDIO_SECONDS,
    check_mp3,
    check_ogg,
    check_wave,
)
from arclight.media.render_plan import AUDIO_RATE, RenderAudioClip, RenderAudioSource, RenderPlan

if TYPE_CHECKING:
    import av


def decoded_frames(source: RenderAudioSource) -> Generator[av.AudioFrame, None, None]:
    import av

    data = base64.b64decode(source.data, validate=True)
    if source.media_type == "audio/flac":
        if len(data) < 42 or data[:4] != b"fLaC":
            raise ValueError("Invalid FLAC input")
    else:
        {"audio/wav": check_wave, "audio/mpeg": check_mp3, "audio/ogg": check_ogg}[
            source.media_type
        ](data)

    def refuse_external(*_args, **_kwargs):
        raise ValueError("External resources are forbidden")

    with av.open(
        BytesIO(data),
        format=AUDIO_FORMATS[source.media_type],
        options={"protocol_whitelist": "pipe", "err_detect": "explode"},
        io_open=refuse_external,
    ) as container:
        if not isinstance(container, av.container.InputContainer):
            raise ValueError("Input container required")
        if len(container.streams) != 1 or len(container.streams.audio) != 1:
            raise ValueError("Exactly one audio stream is required")
        stream = container.streams.audio[0]
        if stream.codec_context.name not in AUDIO_CODECS[source.media_type]:
            raise ValueError("Unsupported audio codec")
        stream.codec_context.thread_count = 1
        signature = None
        resampler = None
        elapsed, samples = Fraction(0), 0
        for count, frame in enumerate(container.decode(stream), 1):
            current = (frame.sample_rate, len(frame.layout.channels), frame.layout.name)
            if not 8000 <= current[0] <= AUDIO_RATE or not 1 <= current[1] <= 2:
                raise ValueError("Unsupported audio sample rate or channels")
            if signature is None:
                signature = current
                # Preserve mono amplitude; duplicate its plane only at the mixing boundary.
                resampler = av.AudioResampler(format="fltp", layout=frame.layout, rate=AUDIO_RATE)
            elif current != signature:
                raise ValueError("Audio sample rate or channel layout changes within the file")
            elapsed += Fraction(frame.samples, frame.sample_rate)
            samples += frame.samples
            if elapsed > MAX_AUDIO_SECONDS or samples > AUDIO_RATE * 600 or count > 30000:
                raise ValueError("Audio exceeds decoding limit")
            frame.pts = None
            assert resampler is not None
            yield from resampler.resample(frame)
        if resampler is None:
            raise ValueError("No decoded audio")
        yield from resampler.resample(None)


class ClipReader:
    def __init__(self, source: RenderAudioSource, clip: RenderAudioClip):
        self.frames = decoded_frames(source)
        self.clip = clip
        self.frame: av.AudioFrame | None = None
        self.left = self.right = memoryview(b"").cast("f")
        self.position = 0
        self.skip = clip.source_start_ms * 48
        self.points = tuple(
            (point.offset_ms * 48, point.gain_milli)
            for point in (clip.audio.envelope if clip.audio else ())
        )
        self.offsets = tuple(point[0] for point in self.points)

    def close(self) -> None:
        self.frames.close()

    def advance(self) -> None:
        try:
            frame = next(self.frames)
        except StopIteration:
            raise ValueError("Audio source has insufficient decoded samples") from None
        self.frame = frame
        self.left = memoryview(frame.planes[0]).cast("f")[: frame.samples]
        self.right = (
            memoryview(frame.planes[1]).cast("f")[: frame.samples]
            if len(frame.layout.channels) == 2
            else self.left
        )
        if not all(isfinite(value) for plane in (self.left, self.right) for value in plane):
            raise ValueError("Non-finite decoded audio samples")
        self.position = 0

    def gain(self, sample: int) -> float:
        automation = self.clip.audio
        if automation is None:
            return 1.0
        gain = automation.gain_milli / 1000
        if self.points:
            index = bisect_right(self.offsets, sample) - 1
            start, low = self.points[index]
            end, high = self.points[index + 1]
            gain *= (low + (high - low) * (sample - start) / (end - start)) / 1000
        if automation.fade_in_ms:
            gain *= min(1.0, sample / (automation.fade_in_ms * 48))
        if automation.fade_out_ms:
            gain *= min(1.0, (self.clip.duration_ms * 48 - sample) / (automation.fade_out_ms * 48))
        return gain

    def mix(self, left: list[float], right: list[float], offset: int, count: int, local: int):
        while self.skip:
            if self.position == len(self.left):
                self.advance()
            take = min(self.skip, len(self.left) - self.position)
            self.position += take
            self.skip -= take
        done = 0
        while done < count:
            if self.position == len(self.left):
                self.advance()
            take = min(count - done, len(self.left) - self.position)
            for index in range(take):
                gain = self.gain(local + done + index)
                left[offset + done + index] += self.left[self.position + index] * gain
                right[offset + done + index] += self.right[self.position + index] * gain
            self.position += take
            done += take


class AudioMixer:
    def __init__(self, plan: RenderPlan, *, output_gain: float = 1.0):
        self.plan = plan
        self.output_gain = output_gain
        self.pending = iter(sorted(enumerate(plan.audio_clips), key=lambda item: item[1].start_ms))
        self.next_clip = next(self.pending, None)
        self.active: dict[int, ClipReader] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        for reader in self.active.values():
            reader.close()

    def frame(self, start: int, count: int):
        import av

        end = start + count
        # Release ended decoders before opening clips that begin on the same sample.
        for key, reader in list(self.active.items()):
            if (reader.clip.start_ms + reader.clip.duration_ms) * 48 <= start:
                reader.close()
                del self.active[key]
        while self.next_clip is not None and self.next_clip[1].start_ms * 48 < end:
            key, clip = self.next_clip
            self.active[key] = ClipReader(self.plan.audio_sources[clip.source_index], clip)
            self.next_clip = next(self.pending, None)
        left, right = [0.0] * count, [0.0] * count
        for key, reader in list(self.active.items()):
            clip = reader.clip
            begin = max(start, clip.start_ms * 48)
            stop = min(end, (clip.start_ms + clip.duration_ms) * 48)
            if stop > begin:
                reader.mix(left, right, begin - start, stop - begin, begin - clip.start_ms * 48)
            if stop == (clip.start_ms + clip.duration_ms) * 48:
                reader.close()
                del self.active[key]
        result = av.AudioFrame(format="fltp", layout="stereo", samples=count)
        peak = max((abs(value) for channel in (left, right) for value in channel), default=0.0)
        # Stereo-linked block limiting preserves wave shape and relative channel levels.
        gain = min(1.0, 0.95 / peak) * self.output_gain if peak else self.output_gain
        for plane, values in zip(result.planes, (left, right), strict=True):
            raw = array("f", (value * gain for value in values)).tobytes()
            plane.update(raw + bytes(plane.buffer_size - len(raw)))
        result.sample_rate = AUDIO_RATE
        result.pts, result.time_base = start, Fraction(1, AUDIO_RATE)
        return result
