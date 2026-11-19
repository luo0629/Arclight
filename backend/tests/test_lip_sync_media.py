"""Exercise actual audio and pixels, not only a successful encode status."""

import base64
import json
import wave
from array import array
from io import BytesIO
from math import pi, sin

import av
import pytest
from PIL import Image, ImageDraw

from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.mp4 import open_mp4
from arclight.media.validation import probe


def pose_video(*, width=128, height=128, rate=10, seconds=2) -> bytes:
    result = BytesIO()
    with av.open(result, mode="w", format="mp4") as mux:
        stream = mux.add_stream("libx264", rate=rate)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.options = {"threads": "1", "preset": "ultrafast"}
        for i in range(rate * seconds):
            image = Image.new("RGB", (128, 128), (228, 224, 219))
            draw = ImageDraw.Draw(image)
            draw.ellipse((32, 17, 96, 108), fill=(224, 173, 145))
            for x in (47, 76):
                draw.ellipse((x, 47, x + 5, 54), fill=(23, 20, 19))
            draw.ellipse((51, 70, 77, 88 if i == 1 else 73), fill=(37, 12, 7))
            x = i % 100
            draw.rectangle((6 + x, 110, 14 + x, 122), fill=(16, 112, 184))
            with image.resize((width, height)) as resized:
                frame = av.VideoFrame.from_image(resized)
            for packet in stream.encode(frame):
                mux.mux(packet)
        for packet in stream.encode(None):
            mux.mux(packet)
    return result.getvalue()


def cue_audio() -> bytes:
    result = BytesIO()
    with wave.open(result, "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        data = array(
            "h",
            (
                round(13_000 * sin(2 * pi * 220 * i / 16_000))
                if 1600 <= i < 4800 or 8000 <= i < 11200
                else 0
                for i in range(12_800)
            ),
        )
        wav.writeframes(data.tobytes())
    return result.getvalue()


MOUTH = {"x": 3750, "y": 5235, "width": 2500, "height": 1875}


def media_plan(video: bytes, audio: bytes, **overrides) -> bytes:
    return json.dumps(
        {
            "video": base64.b64encode(video).decode(),
            "lines": [
                {
                    "start_ms": 400,
                    "duration_ms": 800,
                    "closed_frame_ms": 0,
                    "open_frame_ms": 100,
                    "mouth": MOUTH,
                    "audio": {"data": base64.b64encode(audio).decode(), "media_type": "audio/wav"},
                    **overrides,
                }
            ],
        }
    ).encode()


def video_pixels(data: bytes) -> list[Image.Image]:
    with open_mp4(data) as container:
        return [frame.to_image(threads=1) for frame in container.decode(video=0)]


def test_local_correction_changes_mouth_on_the_exact_audio_clock():
    original = pose_video()
    corrected = execute_media("lip-sync", "mouth-pose", media_plan(original, cue_audio()))
    info = probe(corrected, "video/mp4")
    assert (info["duration_ms"], info["decoded_frames"], info["frame_rate"], info["has_audio"]) == (
        2000,
        20,
        10,
        True,
    )
    source, output = video_pixels(original), video_pixels(corrected)
    # Mouth started closed even during the fixture's spoken segments.
    assert all(source[index].getpixel((64, 81))[0] > 180 for index in (5, 9))
    assert all(output[index].getpixel((64, 81))[0] < 75 for index in (5, 6, 9, 10))
    assert all(output[index].getpixel((64, 81))[0] > 180 for index in (4, 7, 8, 11))
    # A separate moving element retains its position and timing.
    for index in range(20):
        assert output[index].getpixel((10 + index, 115))[2] > 130
    with open_mp4(corrected) as container:
        sample_peaks = {}
        for frame in container.decode(audio=0):
            time_ms = round(frame.time * 1000)
            sample_peaks[time_ms] = max(
                abs(value) for value in memoryview(frame.planes[0]).cast("f")[: frame.samples]
            )
        assert max(amp for ms, amp in sample_peaks.items() if 540 < ms < 640) > 0.2
        assert max(amp for ms, amp in sample_peaks.items() if 760 < ms < 850) < 0.01
        assert max(amp for ms, amp in sample_peaks.items() if 940 < ms < 1040) > 0.2


@pytest.mark.parametrize(
    "overrides", [{"open_frame_ms": 15}, {"start_ms": 1500}, {"duration_ms": 500}]
)
def test_local_correction_rejects_unusable_frames_or_audio_range(overrides):
    with pytest.raises(MediaExecutionError, match="INVALID_MEDIA"):
        execute_media("lip-sync", "mouth-pose", media_plan(pose_video(), cue_audio(), **overrides))
