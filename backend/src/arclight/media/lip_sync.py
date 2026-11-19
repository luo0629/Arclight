"""Render local, audio-driven mouth poses inside the existing media sandbox."""

from __future__ import annotations

import base64
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from math import ceil, isfinite, sqrt
from typing import TYPE_CHECKING, Self

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
from pydantic import Field, model_validator

from arclight.domain.lip_sync import MAX_SYNC_LINES, MAX_SYNC_MS, MouthRegion, SyncContract, Time
from arclight.media.mp4 import open_mp4
from arclight.media.render_plan import AUDIO_RATE, RenderAudioSource
from arclight.media.timeline_mixer import decoded_frames

if TYPE_CHECKING:
    import av

MAX_VIDEO_BYTES = 16 * 1024**2
MAX_AUDIO_BYTES = 4 * 1024**2
MAX_INPUT_BYTES = 28 * 1024**2
MAX_OUTPUT_BYTES = 24 * 1024**2
MAX_SYNC_PIXELS = 1920 * 1080
MAX_PIXEL_FRAMES = 960_000_000
SAMPLES_PER_MS = AUDIO_RATE // 1000


class LineMedia(SyncContract):
    audio: RenderAudioSource
    start_ms: Time
    duration_ms: int = Field(strict=True, gt=0, le=MAX_SYNC_MS)
    closed_frame_ms: Time
    open_frame_ms: Time
    mouth: MouthRegion


class SyncMedia(SyncContract):
    video: str
    lines: tuple[LineMedia, ...] = Field(min_length=1, max_length=MAX_SYNC_LINES)

    @model_validator(mode="after")
    def nonoverlapping_lines(self) -> Self:
        boundary = 0
        for line in self.lines:
            if line.start_ms < boundary:
                raise ValueError("Overlapping or unordered mouth cues")
            boundary = line.start_ms + line.duration_ms
        if boundary > MAX_SYNC_MS:
            raise ValueError("Mouth cues exceed local route duration")
        return self


@dataclass(frozen=True)
class VideoClock:
    rate: Fraction
    frames: int
    width: int
    height: int

    def index(self, timestamp_ms: int) -> int:
        return int(timestamp_ms * self.rate / 1000)

    def sample(self, index: int) -> int:
        return ceil(index * AUDIO_RATE / self.rate)


@dataclass(frozen=True)
class MouthPoses:
    box: tuple[int, int, int, int]
    closed: Image.Image
    opened: Image.Image
    mask: Image.Image

    def apply(self, image: Image.Image, weight: float) -> None:
        image.paste(Image.blend(self.closed, self.opened, weight), self.box, self.mask)


def video_clock(data: bytes) -> VideoClock:
    with open_mp4(data) as container:
        if len(container.streams.video) != 1 or len(container.streams) > 2:
            raise ValueError("Expected one video stream")
        stream = container.streams.video[0]
        rate, count = stream.average_rate, stream.frames
        width, height = stream.width, stream.height
        if (
            rate is None
            or not 8 <= rate <= 60
            or not 2 <= count <= MAX_SYNC_MS * rate / 1000
            or min(width, height) < 32
            or max(width, height) > 1920
            or width % 2
            or height % 2
            or width * height > MAX_SYNC_PIXELS
            or width * height * count > MAX_PIXEL_FRAMES
        ):
            raise ValueError("Video exceeds local mouth-pose work limits")
        return VideoClock(rate, count, width, height)


def frames(data: bytes, clock: VideoClock) -> Iterator[tuple[int, av.VideoFrame]]:
    with open_mp4(data) as container:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 1
        count = 0
        for index, frame in enumerate(container.decode(stream)):
            if (
                index >= clock.frames
                or frame.is_corrupt
                or (frame.width, frame.height) != (clock.width, clock.height)
                or frame.pts is None
                or frame.time_base is None
                or abs(frame.pts * frame.time_base - index / clock.rate) > Fraction(1, 1000)
            ):
                raise ValueError("Mouth-pose route requires a zero-based constant video clock")
            count += 1
            yield index, frame
        if count != clock.frames:
            raise ValueError("Source video frame count changed")


def capture_poses(data: bytes, clock: VideoClock, lines: tuple[LineMedia, ...]) -> list[MouthPoses]:
    requests: dict[int, list[tuple[int, int, tuple[int, int, int, int]]]] = {}
    patches: dict[tuple[int, int], Image.Image] = {}
    boxes = []
    for ordinal, line in enumerate(lines):
        rect = line.mouth
        box = (
            rect.x * clock.width // 10000,
            rect.y * clock.height // 10000,
            (rect.x + rect.width) * clock.width // 10000,
            (rect.y + rect.height) * clock.height // 10000,
        )
        keys = [clock.index(line.closed_frame_ms), clock.index(line.open_frame_ms)]
        if len(set(keys)) != 2 or max(keys) >= clock.frames:
            raise ValueError("Pose timestamps must resolve to distinct exact video frames")
        if min(box[2] - box[0], box[3] - box[1]) < 8:
            raise ValueError("Mouth crop is too small")
        boxes.append(box)
        for pose, index in enumerate(keys):
            requests.setdefault(index, []).append((ordinal, pose, box))
    for index, frame in frames(data, clock):
        if index in requests:
            with frame.to_image(threads=1) as image:
                for ordinal, pose, box in requests[index]:
                    patches[ordinal, pose] = image.crop(box)
    result = []
    for ordinal, box in enumerate(boxes):
        closed, opened = patches[ordinal, 0], patches[ordinal, 1]
        if max(ImageStat.Stat(ImageChops.difference(closed, opened)).mean) < 2:
            raise ValueError("Open and closed mouth poses have no meaningful visual difference")
        width, height = closed.size
        edge = max(1, min(width, height) // 10)
        mask = Image.new("L", closed.size)
        ImageDraw.Draw(mask).rectangle((edge, edge, width - 1 - edge, height - 1 - edge), fill=255)
        result.append(
            MouthPoses(box, closed, opened, mask.filter(ImageFilter.GaussianBlur(edge / 2)))
        )
    return result


def soundtrack(lines: tuple[LineMedia, ...], total: int) -> tuple[array[float], array[float]]:
    left, right = array("f", [0.0]) * total, array("f", [0.0]) * total
    for line in lines:
        start = line.start_ms * SAMPLES_PER_MS
        duration = line.duration_ms * SAMPLES_PER_MS
        if start + duration > total:
            raise ValueError("Exact dialogue is longer than the selected video")
        offset, voice_peak = 0, 0.0
        for frame in decoded_frames(line.audio):
            count = max(0, min(frame.samples, duration - offset))
            for output, plane in zip((left, right), (0, len(frame.planes) - 1), strict=True):
                values = memoryview(frame.planes[plane]).cast("f")[:count]
                for i, value in enumerate(values, start=start + offset):
                    if not isfinite(value):
                        raise ValueError("Non-finite decoded dialogue")
                    output[i] = value
                    voice_peak = max(voice_peak, abs(value))
            offset += frame.samples
            if offset > duration + SAMPLES_PER_MS:
                raise ValueError("Decoded dialogue duration differs from its exact receipt")
        if abs(offset - duration) > SAMPLES_PER_MS or voice_peak < 0.003:
            raise ValueError("No audible dialogue matching the exact receipt")
    return left, right


def mouth_weights(
    clock: VideoClock, lines: tuple[LineMedia, ...], tracks: tuple[array[float], array[float]]
) -> dict[int, tuple[int, float]]:
    """Drive the frame clock with the same samples that are muxed into the candidate."""
    weights: dict[int, tuple[int, float]] = {}
    for ordinal, line in enumerate(lines):
        end_ms = line.start_ms + line.duration_ms
        start, end = line.start_ms * SAMPLES_PER_MS, end_ms * SAMPLES_PER_MS
        levels = []
        for index in range(clock.index(line.start_ms), min(clock.frames, clock.index(end_ms) + 1)):
            low, high = max(start, clock.sample(index)), min(end, clock.sample(index + 1))
            if low < high:
                level = max(
                    sqrt(sum(sample * sample for sample in track[low:high]) / (high - low))
                    for track in tracks
                )
                levels.append((index, level))
        peak = max((level for _, level in levels), default=0.0)
        if peak < 0.006:
            raise ValueError("The line is not audible at the video frame clock")
        gate = max(0.002, peak * 0.08)
        for index, level in levels:
            weight = max(0.0, min(1.0, (level - gate) / (peak * 0.5 - gate)))
            if index not in weights or weights[index][1] < weight:
                # Sub-frame line boundaries allow one visible pose on that output frame.
                weights[index] = (ordinal, weight)
    return weights


def encode_lip_sync(raw: bytes) -> bytes:
    import av

    plan = SyncMedia.model_validate_json(raw)
    source = base64.b64decode(plan.video, validate=True)
    if (
        not 0 < len(source) <= MAX_VIDEO_BYTES
        or sum(len(line.audio.data) for line in plan.lines)
        > (MAX_AUDIO_BYTES + 2 * MAX_SYNC_LINES) * 4 // 3 + 4
    ):
        raise ValueError("Mouth-pose media exceeds its byte limit")
    clock = video_clock(source)
    tracks = soundtrack(plan.lines, clock.sample(clock.frames))
    poses = capture_poses(source, clock, plan.lines)
    weights = mouth_weights(clock, plan.lines, tracks)
    output = BytesIO()
    with av.open(output, mode="w", format="mp4") as container:
        video = container.add_stream("libx264", rate=clock.rate)
        video.width, video.height = clock.width, clock.height
        video.pix_fmt = "yuv420p"
        video.options = {"preset": "ultrafast", "threads": "1", "crf": "18"}
        audio = container.add_stream("aac", rate=AUDIO_RATE)
        audio.layout = "stereo"
        audio.codec_context.thread_count = 1
        audio.bit_rate = 128000
        cursor = 0
        for index, original in frames(source, clock):
            if index in weights:
                pose, weight = weights[index]
                with original.to_image(threads=1) as image:
                    poses[pose].apply(image, weight)
                    frame = av.VideoFrame.from_image(image)
            else:
                frame = original
                frame.pict_type = 0
            frame.pts, frame.time_base = index, 1 / clock.rate
            for packet in video.encode(frame):
                container.mux(packet)
            target = clock.sample(index + 1)
            while cursor < target:
                end = min(target, cursor + 1024)
                pcm = av.AudioFrame(format="fltp", layout="stereo", samples=end - cursor)
                pcm.pts, pcm.time_base, pcm.sample_rate = (
                    cursor,
                    Fraction(1, AUDIO_RATE),
                    AUDIO_RATE,
                )
                for track, plane in zip(tracks, pcm.planes, strict=True):
                    plane.update(track[cursor:end].tobytes())
                for packet in audio.encode(pcm):
                    container.mux(packet)
                cursor = end
            if output.tell() > MAX_OUTPUT_BYTES:
                raise ValueError("Corrected media exceeds its byte limit")
        for stream in (video, audio):
            for packet in stream.encode(None):
                container.mux(packet)
    return output.getvalue()
