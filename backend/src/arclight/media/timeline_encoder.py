"""Fixed PyAV/FFmpeg encoder. Inputs and outputs are bytes; no media paths or expressions."""

import base64
import sys
from fractions import Fraction
from io import BytesIO

from arclight.media.render_plan import MAX_OUTPUT_BYTES, RenderClip, RenderPlan


class BoundedOutput(BytesIO):
    def write(self, data, /):
        if self.tell() + len(data) > MAX_OUTPUT_BYTES:
            raise ValueError("Render output limit exceeded")
        return super().write(data)


def viewport(image, clip: RenderClip, local_ms: Fraction, size: tuple[int, int], fps: int):
    from PIL import Image, ImageEnhance, ImageOps

    start, end = clip.visual.crop, clip.visual.motion.end_crop or clip.visual.crop
    fraction = min(Fraction(1), local_ms / max(Fraction(1), clip.duration_ms - Fraction(1000, fps)))
    values = [
        float(getattr(start, name) + (getattr(end, name) - getattr(start, name)) * fraction)
        for name in ("x", "y", "width", "height")
    ]
    x, y, width, height = values
    left, top = int(image.width * x / 10000), int(image.height * y / 10000)
    right = min(image.width, max(left + 1, round(image.width * (x + width) / 10000)))
    bottom = min(image.height, max(top + 1, round(image.height * (y + height) / 10000)))
    result = ImageOps.pad(
        image.crop((left, top, right, bottom)), size, method=Image.Resampling.BICUBIC
    )
    brightness = min(
        1.0,
        float(local_ms / clip.fade_in_ms) if clip.fade_in_ms else 1.0,
        float((clip.duration_ms - local_ms) / clip.fade_out_ms) if clip.fade_out_ms else 1.0,
    )
    return ImageEnhance.Brightness(result).enhance(max(0.0, brightness))


def encode_timeline(data: bytes) -> bytes:
    import av
    from PIL import Image

    plan = RenderPlan.model_validate_json(data)
    profile = plan.output
    output = BoundedOutput()
    with av.open(output, mode="w", format="mp4") as container:
        video = container.add_stream("libx264", rate=profile.fps)
        video.width, video.height = profile.width, profile.height
        video.pix_fmt = profile.pixel_format
        video.bit_rate = profile.video_bitrate
        video.time_base = Fraction(1, profile.fps)
        video.options = {"preset": "ultrafast", "threads": "1"}
        audio = container.add_stream("aac", rate=48000)
        audio.layout = "stereo"
        audio.bit_rate = profile.audio_bitrate
        audio.codec_context.thread_count = 1
        total_samples = (plan.frames * 48000 + profile.fps - 1) // profile.fps
        sample = 0
        clip_index, image, last_progress = -1, None, -1
        for index in range(plan.frames):
            stamp = Fraction(index * 1000, profile.fps)
            next_index = max(i for i, item in enumerate(plan.clips) if item.start_ms <= stamp)
            if next_index != clip_index:
                if image is not None:
                    image.close()
                clip_index = next_index
                clip = plan.clips[clip_index]
                raw = base64.b64decode(clip.image, validate=True)
                if not 0 < len(raw) <= 1024**2:
                    raise ValueError("Invalid image size")
                with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as source:
                    if source.width * source.height > 4_000_000 or max(source.size) > 4096:
                        raise ValueError("Invalid image dimensions")
                    image = source.convert("RGB")
            clip = plan.clips[clip_index]
            with viewport(
                image, clip, stamp - clip.start_ms, (profile.width, profile.height), profile.fps
            ) as rendered:
                frame = av.VideoFrame.from_image(rendered)
            frame.pts, frame.time_base = index, Fraction(1, profile.fps)
            for packet in video.encode(frame):
                container.mux(packet)
            # Interleave a bounded amount of explicit silence with every video frame.
            target = min(total_samples, ((index + 1) * 48000 + profile.fps - 1) // profile.fps)
            while sample < target:
                count = min(1024, target - sample)
                silence = av.AudioFrame(format="fltp", layout="stereo", samples=count)
                for plane in silence.planes:
                    plane.update(bytes(plane.buffer_size))
                silence.sample_rate = 48000
                silence.pts, silence.time_base = sample, Fraction(1, 48000)
                for packet in audio.encode(silence):
                    container.mux(packet)
                sample += count
            progress = (index + 1) * 100 // plan.frames
            if progress // 5 != last_progress:
                last_progress = progress // 5
                sys.stderr.write(f"ARCLIGHT_PROGRESS:{progress}\n")
                sys.stderr.flush()
        for stream in (video, audio):
            for packet in stream.encode(None):
                container.mux(packet)
        if image is not None:
            image.close()
    return output.getvalue()
