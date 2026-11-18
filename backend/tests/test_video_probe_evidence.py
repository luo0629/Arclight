"""VIDEO-01 evidence is derived from real encoded video and audio streams."""

import io
import math
from array import array
from fractions import Fraction

import pytest

from arclight.core.errors import DomainError
from arclight.media.validation import probe


@pytest.fixture(scope="module")
def muxed_video():
    import av

    result = io.BytesIO()
    with av.open(result, "w", format="mp4") as container:
        video = container.add_stream("mpeg4", rate=24)
        video.width, video.height, video.pix_fmt = 320, 180, "yuv420p"
        audio = container.add_stream("aac", rate=24000)
        audio.layout = "mono"
        image = av.VideoFrame(320, 180, "yuv420p")
        for plane in image.planes:
            plane.update(bytes(plane.buffer_size))
        for index in range(12):
            image.pts = index
            for packet in video.encode(image):
                container.mux(packet)
            sound = av.AudioFrame(format="fltp", layout="mono", samples=1024)
            sound.sample_rate, sound.time_base, sound.pts = 24000, Fraction(1, 24000), index * 1024
            sound.planes[0].update(
                array(
                    "f",
                    (
                        0.1 * math.sin(2 * math.pi * 440 * (index * 1024 + sample) / 24000)
                        for sample in range(1024)
                    ),
                ).tobytes()
            )
            for packet in audio.encode(sound):
                container.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode():
                container.mux(packet)
    return result.getvalue()


def test_actual_mp4_audio_codec_and_full_decode_evidence(muxed_video):
    import av

    with av.open(io.BytesIO(muxed_video)) as container:
        codec = container.streams.video[0].codec_context.name
        assert container.streams.audio[0].codec_context.name == "aac"
        frames = list(container.decode())
    video_frames = [frame for frame in frames if isinstance(frame, av.VideoFrame)]
    audio_frames = [frame for frame in frames if isinstance(frame, av.AudioFrame)]
    assert len(video_frames) == 12
    assert audio_frames and sum(frame.samples for frame in audio_frames) > 0
    assert all(frame.sample_rate == 24000 for frame in audio_frames)
    assert any(any(bytes(frame.planes[0])) for frame in audio_frames)

    actual = probe(muxed_video, "video/mp4")
    assert actual["width"] == 320 and actual["height"] == 180
    assert actual["duration_ms"] == 500 and actual["frame_rate"] == 24
    assert actual["codec"] == codec == "mpeg4"
    assert actual["decoded_frames"] == len(video_frames)
    assert actual["has_audio"] is True
    assert actual["full_decode_verified"] is True


@pytest.mark.parametrize("truncation", ["middle", "last_byte"])
def test_muxed_mp4_truncation_cannot_claim_complete_decode(muxed_video, truncation):
    truncated = muxed_video[: len(muxed_video) // 2] if truncation == "middle" else muxed_video[:-1]
    with pytest.raises(DomainError) as caught:
        probe(truncated, "video/mp4")
    assert caught.value.code == "INVALID_FILE" and caught.value.status_code == 422


def test_mp4_valid_boxes_with_corrupt_encoded_payload_are_rejected(muxed_video):
    offset = 0
    while offset < len(muxed_video):
        size = int.from_bytes(muxed_video[offset : offset + 4], "big")
        assert size >= 8
        if muxed_video[offset + 4 : offset + 8] == b"mdat":
            damaged = muxed_video[: offset + 8] + bytes(size - 8) + muxed_video[offset + size :]
            break
        offset += size
    else:
        pytest.fail("The real muxed fixture must contain encoded media data")
    # Keep the original container/index intact; actual decoder failure must
    # prevent a nominally complete MP4 from receiving full-decode evidence.
    assert len(damaged) == len(muxed_video)
    with pytest.raises(DomainError) as caught:
        probe(damaged, "video/mp4")
    assert caught.value.code == "INVALID_FILE" and caught.value.status_code == 422
