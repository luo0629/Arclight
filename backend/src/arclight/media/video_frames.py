"""Extract bounded, time-stamped visual evidence inside the media worker."""

import base64
import json
from hashlib import sha256
from io import BytesIO

from arclight.media.mp4 import open_mp4

SAMPLE_COUNT = 8
SAMPLE_SIDE = 384
SAMPLE_OUTPUT_LIMIT = 768 * 1024


def sample_video(data: bytes) -> bytes:
    from PIL import Image

    with open_mp4(data) as container:
        if len(container.streams.video) != 1 or not 0 < (container.duration or 0) <= 30_000_000:
            raise ValueError("Video does not meet sampling bounds")
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 1
        samples: dict[int, Image.Image] = {}
        last_ms, count, bucket = -1, 0, 0
        for frame in container.decode(video=0):
            count += 1
            if frame.pts is None or frame.time_base is None or count > 3600:
                raise ValueError("Invalid decoded video timestamps")
            time_ms = round(1000 * frame.pts * frame.time_base)
            if not 0 <= time_ms < 30000 or time_ms < last_ms:
                raise ValueError("Video frame outside the sampling window")
            if max(frame.width, frame.height) > 4096 or frame.width * frame.height > 8_294_400:
                raise ValueError("Video frame exceeds sampling dimensions")
            next_bucket = min(
                SAMPLE_COUNT - 2, time_ms * 1000 * (SAMPLE_COUNT - 1) // container.duration
            )
            if count == 1 or next_bucket > bucket:
                samples[time_ms] = thumbnail(frame)
                bucket = next_bucket
            last_ms = time_ms
        if not count:
            raise ValueError("No video frames")
        if last_ms not in samples:
            samples[last_ms] = thumbnail(frame)

    first = next(iter(samples.values()))
    width, height = first.size
    sheet = Image.new("RGB", (width * 2, height * ((len(samples) + 1) // 2)))
    timeline = []
    for ordinal, (time_ms, image) in enumerate(samples.items()):
        if image.size != first.size:
            raise ValueError("Video changed dimensions during sampling")
        sheet.paste(image, (width * (ordinal % 2), height * (ordinal // 2)))
        timeline.append({"time_ms": time_ms, "frame_sha256": sha256(image.tobytes()).hexdigest()})
    stream_bytes = BytesIO()
    sheet.save(stream_bytes, format="JPEG", quality=80)
    return json.dumps(
        {
            "decoded_frames": count,
            "samples": timeline,
            "sheet": base64.b64encode(stream_bytes.getvalue()).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode()


def thumbnail(frame):
    scale = min(1, SAMPLE_SIDE / max(frame.width, frame.height))
    return frame.to_image(
        width=max(1, round(frame.width * scale)), height=max(1, round(frame.height * scale))
    )
