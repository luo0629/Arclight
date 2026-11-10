"""Decode every packet in a sealed worker; native metadata never becomes diagnostics."""

from fractions import Fraction
from typing import Literal, cast

from arclight.domain.render_inspections import (
    AudioMeasurement,
    RenderMediaMeasurement,
    VideoMeasurement,
)
from arclight.media.mp4 import open_mp4
from arclight.media.render_plan import MAX_FRAMES, MAX_PIXEL_FRAMES


def inspect_render(data: bytes) -> RenderMediaMeasurement:
    import av

    with open_mp4(data) as container:
        if not isinstance(container, av.container.InputContainer):
            raise ValueError("Input container required")
        kinds = tuple(
            cast(
                Literal["video", "audio", "subtitle", "data", "attachment", "other"],
                s.type
                if s.type in {"video", "audio", "subtitle", "data", "attachment"}
                else "other",
            )
            for s in container.streams
        )
        if (
            len(kinds) > 16
            or container.duration is None
            or not 0 < container.duration <= 3_600_000_000
        ):
            raise ValueError("Invalid container bounds")
        if sorted(kinds) != ["audio", "video"]:
            return RenderMediaMeasurement(
                streams=kinds,
                container_duration_us=container.duration,
                decodable=False,
                video=None,
                audio=None,
            )
        video, audio = container.streams.video[0], container.streams.audio[0]
        vc, ac = video.codec_context, audio.codec_context
        average_rate = video.average_rate
        rate = (
            Fraction(average_rate.numerator, average_rate.denominator)
            if average_rate is not None
            else Fraction(0)
        )
        width, height = vc.width, vc.height
        if (
            not 0 < rate <= 120
            or not 0 < width * height <= 8_294_400
            or max(width, height) > 4096
            or not 8000 <= ac.sample_rate <= 48000
            or not 1 <= len(ac.layout.channels) <= 2
            or vc.name not in {"h264", "hevc", "av1", "mpeg4"}
            or ac.name != "aac"
            or video.duration is None
            or audio.duration is None
        ):
            raise ValueError("Unsupported decoding bounds")
        for stream in container.streams:
            stream.codec_context.thread_count = 1
        frames, samples = 0, 0
        video_end, audio_end = Fraction(0), Fraction(0)
        video_timing, audio_timing = True, True
        pixel_format: Literal["yuv420p", "other"] = (
            "yuv420p" if vc.pix_fmt == "yuv420p" else "other"
        )
        for packet in container.demux():
            if packet.is_corrupt:
                raise ValueError("Corrupt packet")
            for frame in packet.decode():
                if not isinstance(frame, av.VideoFrame | av.AudioFrame):
                    raise ValueError("Unexpected frame")
                if frame.is_corrupt or frame.pts is None or frame.time_base is None:
                    raise ValueError("Invalid decoded frame")
                timestamp = frame.pts * frame.time_base
                if isinstance(frame, av.VideoFrame):
                    if (frame.width, frame.height) != (width, height):
                        raise ValueError("Variable dimensions")
                    video_timing &= abs(timestamp - Fraction(frames, rate)) <= frame.time_base
                    if frame.format.name != "yuv420p":
                        pixel_format = "other"
                    frames += 1
                    video_end = timestamp + 1 / rate
                    if (
                        frames > MAX_FRAMES
                        or frames * width * height > MAX_PIXEL_FRAMES
                        or not 0 <= timestamp < 3600
                    ):
                        raise ValueError("Video workload exceeded")
                elif isinstance(frame, av.AudioFrame):
                    if frame.sample_rate != ac.sample_rate or len(frame.layout.channels) != len(
                        ac.layout.channels
                    ):
                        raise ValueError("Variable audio format")
                    audio_timing &= (
                        abs(timestamp - Fraction(samples, ac.sample_rate)) <= frame.time_base
                    )
                    samples += frame.samples
                    audio_end = timestamp + Fraction(frame.samples, frame.sample_rate)
                    if samples > 48000 * 3600 + 1024 or not 0 <= timestamp <= 3600:
                        raise ValueError("Audio workload exceeded")
                else:
                    raise ValueError("Unexpected frame")
        return RenderMediaMeasurement(
            streams=kinds,
            container_duration_us=container.duration,
            decodable=bool(frames and samples),
            video=VideoMeasurement(
                width=width,
                height=height,
                rate_numerator=rate.numerator,
                rate_denominator=rate.denominator,
                codec=cast(Literal["h264", "hevc", "av1", "mpeg4", "other"], vc.name),
                pixel_format=pixel_format,
                frames=frames,
                duration_us=round(video_end * 1_000_000),
                declared_duration_us=round(video.duration * video.time_base * 1_000_000),
                timing_valid=video_timing,
            ),
            audio=AudioMeasurement(
                codec="aac",
                sample_rate=ac.sample_rate,
                channels=len(ac.layout.channels),
                samples=samples,
                duration_us=round(audio_end * 1_000_000),
                declared_duration_us=round(audio.duration * audio.time_base * 1_000_000),
                timing_valid=audio_timing,
            ),
        )
