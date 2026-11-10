"""Decode bounded generated MP4 in the existing isolated parser process."""

from arclight.media.mp4 import open_mp4


def inspect_video(data: bytes) -> dict:
    import av

    with open_mp4(data) as container:
        if not isinstance(container, av.container.InputContainer):
            raise ValueError("Input container required")
        if len(container.streams.video) != 1 or len(container.streams) > 2:
            raise ValueError("Exactly one video and optional audio stream are required")
        stream = container.streams.video[0]
        rate = float(stream.average_rate or 0)
        width, height = stream.codec_context.width, stream.codec_context.height
        if not 0 < rate <= 120 or not 0 < width * height <= 8_294_400 or max(width, height) > 4096:
            raise ValueError("Unsupported video dimensions or rate")
        if stream.codec_context.name not in {"h264", "hevc", "av1", "mpeg4"}:
            raise ValueError("Unsupported video codec")
        for item in container.streams:
            item.codec_context.thread_count = 1
            if item.type not in {"audio", "video"}:
                raise ValueError("Unsupported extra stream")
        if container.duration is None or not 0 < container.duration <= 30_000_000:
            raise ValueError("Unsupported video duration")
        frames, samples, end_time, previous_time = 0, 0, 0.0, -1.0
        for frame in container.decode():
            if isinstance(frame, av.VideoFrame):
                frames += 1
                timestamp = frame.time
                if (
                    (frame.width, frame.height) != (width, height)
                    or frames > 3600
                    or frames / rate > 30
                    or timestamp is None
                    or not 0 <= timestamp <= 30 - 1 / rate
                    or timestamp < previous_time
                ):
                    raise ValueError("Video exceeds decoding limits")
                previous_time = timestamp
                end_time = max(end_time, timestamp + 1 / rate)
            else:
                if (
                    not isinstance(frame, av.AudioFrame)
                    or not 8000 <= frame.sample_rate <= 48000
                    or len(frame.layout.channels) > 2
                ):
                    raise ValueError("Unsupported audio stream")
                samples += frame.samples
                if samples > 48000 * 30:
                    raise ValueError("Audio exceeds decoding limits")
        if not frames:
            raise ValueError("No decoded video")
    return {
        "media_type": "video/mp4",
        "width": width,
        "height": height,
        "duration_ms": round(end_time * 1000),
        "frame_rate": rate,
    }
