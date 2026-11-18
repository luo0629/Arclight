"""Decode exact MP4 trims on the shared video and audio output clocks."""

from __future__ import annotations

import base64
from collections.abc import Generator, Iterable
from contextlib import closing
from fractions import Fraction
from typing import TYPE_CHECKING

from arclight.media.execution import MediaExecutionError
from arclight.media.mp4 import open_mp4
from arclight.media.render_plan import AUDIO_RATE, RenderVideo

if TYPE_CHECKING:
    import av
    from PIL.Image import Image


def timed_frames(container, stream):
    previous = Fraction(-1)
    size = (stream.codec_context.width, stream.codec_context.height)
    for count, frame in enumerate(container.decode(stream), 1):
        if frame.pts is None or frame.time_base is None:
            raise ValueError("Missing video presentation timestamp")
        time_ms = 1000 * frame.pts * frame.time_base
        if not 0 <= time_ms < 30000 or time_ms < previous or count > 3600:
            raise ValueError("Video timestamps exceed decoding bounds")
        if (frame.width, frame.height) != size:
            raise ValueError("Video dimensions changed during decoding")
        previous = time_ms
        yield time_ms, frame


def video_images(
    video: RenderVideo, local_stamps: Iterable[Fraction]
) -> Generator[Image, None, None]:
    from PIL import Image as Pillow

    with open_mp4(base64.b64decode(video.data, validate=True)) as container:
        if len(container.streams.video) != 1 or len(container.streams) > 2:
            raise ValueError("Unexpected video streams")
        stream = container.streams.video[0]
        width, height = stream.codec_context.width, stream.codec_context.height
        if (
            not 0 < width * height <= 8_294_400
            or max(width, height) > 4096
            or float(stream.average_rate or 0) != video.frame_rate
            or stream.codec_context.name not in {"h264", "hevc", "av1", "mpeg4"}
        ):
            raise ValueError("Video differs from the admitted format")
        stream.codec_context.thread_count = 1
        frames = timed_frames(container, stream)
        pending = next(frames, None)
        current = None
        first_pixels = None
        moving = False
        image = None
        try:
            for local_ms in local_stamps:
                stamp = video.source_start_ms + local_ms
                next_frame = current
                while pending is not None and pending[0] <= stamp:
                    next_frame = pending
                    pending = next(frames, None)
                if next_frame is None:
                    raise MediaExecutionError("SOURCE_RANGE_INVALID")
                if next_frame is not current:
                    current = next_frame
                    if image is not None:
                        image.close()
                    image = current[1].to_image(threads=1)
                    if video.motion_required and not moving:
                        # Compare the decoded frames shown on the output clock before effects.
                        with image.resize((64, 64), Pillow.Resampling.BOX) as sample:
                            pixels = sample.tobytes()
                        if first_pixels is None:
                            first_pixels = pixels
                        else:
                            # Ignore small codec fluctuations in an otherwise still picture.
                            moving = (
                                sum(
                                    abs(a - b) >= 12
                                    for a, b in zip(first_pixels, pixels, strict=True)
                                )
                                >= 48
                            )
                assert image is not None
                yield image
            if current is not None and pending is None:
                time_ms, frame = current
                frame_duration_ms = (
                    1000 * frame.duration * frame.time_base
                    if frame.duration and frame.time_base
                    else Fraction(1000, 1) / stream.average_rate
                )
                if video.source_end_ms > time_ms + frame_duration_ms + Fraction(1, 2):
                    raise MediaExecutionError("SOURCE_RANGE_INVALID")
            if video.motion_required and not moving:
                raise MediaExecutionError("DYNAMIC_MEDIA_REQUIRED")
        finally:
            frames.close()
            if image is not None:
                image.close()


def timed_audio(video: RenderVideo) -> Generator[tuple[int, av.AudioFrame], None, None]:
    import av

    with open_mp4(base64.b64decode(video.data, validate=True)) as container:
        if len(container.streams.audio) != 1 or len(container.streams.video) != 1:
            raise ValueError("Admitted video must contain the retained audio stream")
        stream = container.streams.audio[0]
        stream.codec_context.thread_count = 1
        resampler = None
        position, samples = 0, 0
        expected = None
        signature = None
        for frame in container.decode(stream):
            current = (frame.sample_rate, frame.layout.name)
            if (
                frame.pts is None
                or frame.time_base is None
                or not 8000 <= frame.sample_rate <= AUDIO_RATE
                or not 1 <= len(frame.layout.channels) <= 2
                or (signature is not None and current != signature)
            ):
                raise ValueError("Invalid embedded audio timestamp or format")
            signature = current
            samples += frame.samples
            if samples > AUDIO_RATE * 30:
                raise ValueError("Embedded audio exceeds decoding bounds")
            stamp = frame.pts * frame.time_base
            if resampler is None or abs(stamp - expected) > Fraction(1, frame.sample_rate):
                # Flush the previous contiguous region before placing the next one by its PTS.
                if resampler is not None:
                    for output in resampler.resample(None):
                        yield position, output
                        position += output.samples
                resampler = av.AudioResampler(
                    format="fltp", layout=frame.layout, rate=AUDIO_RATE, threads=1
                )
                position = round(stamp * AUDIO_RATE)
            expected = stamp + Fraction(frame.samples, frame.sample_rate)
            frame.pts = None
            for output in resampler.resample(frame):
                yield position, output
                position += output.samples
        if resampler is None:
            raise ValueError("Retained audio stream did not decode")
        for output in resampler.resample(None):
            yield position, output
            position += output.samples


def silence(samples: int) -> Generator[av.AudioFrame, None, None]:
    import av

    while samples > 0:
        count = min(samples, 4096)
        frame = av.AudioFrame(format="fltp", layout="mono", samples=count)
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        yield frame
        samples -= count


def embedded_audio_frames(video: RenderVideo) -> Generator[av.AudioFrame, None, None]:
    import av

    cursor = 0
    limit = video.source_end_ms * (AUDIO_RATE // 1000)
    with closing(timed_audio(video)) as frames:
        for start, frame in frames:
            if start >= limit or cursor == limit:
                break
            if start > cursor:
                yield from silence(start - cursor)
                cursor = start
            offset = cursor - start
            take = min(frame.samples - offset, limit - cursor)
            if take <= 0:
                continue
            if offset == 0 and take == frame.samples:
                yield frame
            else:
                section = av.AudioFrame(format="fltp", layout=frame.layout, samples=take)
                for target, plane in zip(section.planes, frame.planes, strict=True):
                    data = bytes(memoryview(plane).cast("f")[offset : offset + take])
                    target.update(data + bytes(target.buffer_size - len(data)))
                yield section
            cursor += take
    yield from silence(limit - cursor)
