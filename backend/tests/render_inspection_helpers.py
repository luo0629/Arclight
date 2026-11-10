"""Small real MP4 fixtures, including deliberate differences from the output profile."""

from fractions import Fraction
from io import BytesIO

import av


def movie(
    *,
    width=256,
    height=256,
    fps=24,
    frames=12,
    video_codec="libx264",
    pixel_format="yuv420p",
    audio_codec="aac",
    sample_rate=48000,
    layout="stereo",
    audio=True,
    video=True,
    audio_tracks=1,
    audio_frames=None,
    video_offset=0,
    audio_offset=0,
):
    output = BytesIO()
    with av.open(output, mode="w", format="mp4") as container:
        streams = []
        if video:
            visual = container.add_stream(video_codec, rate=fps)
            visual.width, visual.height, visual.pix_fmt = width, height, pixel_format
            visual.codec_context.thread_count = 1
            if video_codec == "libx264":
                visual.options = {"preset": "ultrafast", "threads": "1"}
            streams.append(visual)
        sounds = []
        if audio:
            for _ in range(audio_tracks):
                sound = container.add_stream(audio_codec, rate=sample_rate)
                sound.layout = layout
                sound.codec_context.thread_count = 1
                sounds.append(sound)
                streams.append(sound)
        sample = 0
        total = (frames if audio_frames is None else audio_frames) * sample_rate // fps
        for index in range(frames):
            if video:
                frame = av.VideoFrame(width, height, "rgb24")
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                frame.pts, frame.time_base = index + video_offset, Fraction(1, fps)
                for packet in visual.encode(frame):
                    container.mux(packet)
            target = min(total, (index + 1) * sample_rate // fps)
            while sample < target:
                count = min(1024, target - sample)
                for sound in sounds:
                    frame = av.AudioFrame(format="fltp", layout=layout, samples=count)
                    for plane in frame.planes:
                        plane.update(bytes(plane.buffer_size))
                    frame.sample_rate = sample_rate
                    frame.pts, frame.time_base = sample + audio_offset, Fraction(1, sample_rate)
                    for packet in sound.encode(frame):
                        container.mux(packet)
                sample += count
        for stream in streams:
            for packet in stream.encode(None):
                container.mux(packet)
    return output.getvalue()
