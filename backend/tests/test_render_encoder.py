"""Real sealed FFmpeg output preserves visual edits and rejects executable inputs."""

import base64
import json
from io import BytesIO

import av
import pytest
from PIL import Image
from pydantic import ValidationError

from arclight.domain.render_manifests import RenderOutputProfile
from arclight.domain.timeline_visuals import ClipVisual
from arclight.media.execution import MediaExecutionError, command_for, execute_media
from arclight.media.render_plan import RenderClip, RenderPlan


def image_bytes(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def profile(**changes):
    return RenderOutputProfile(
        **{
            "name": "PREVIEW",
            "width": 256,
            "height": 256,
            "fps": 24,
            "video_bitrate": 1_000_000,
            "audio_bitrate": 64_000,
            **changes,
        }
    )


def clip(image, start=0, **changes):
    return RenderClip(
        **{
            "image": image_bytes(image),
            "start_ms": start,
            "duration_ms": 500,
            "visual": ClipVisual(),
            "fade_in_ms": 0,
            "fade_out_ms": 0,
            **changes,
        }
    )


def test_sealed_render_decodes_order_crop_motion_fades_silence_and_progress():
    red = Image.new("RGB", (100, 100), (230, 20, 10))
    blue = Image.new("RGB", (100, 100), (10, 20, 230))
    split = Image.new("RGB", (100, 100), (230, 20, 10))
    split.paste((10, 230, 20), (50, 0, 100, 100))
    gradient = Image.new("RGB", (100, 100))
    gradient.putdata(
        [(255 - x * 255 // 99, x * 255 // 99, 0) for _ in range(100) for x in range(100)]
    )
    cropped = ClipVisual.model_validate(
        {"crop": {"x": 5000, "y": 0, "width": 5000, "height": 10000}}
    )
    motion = ClipVisual.model_validate(
        {
            "crop": {"x": 1000, "y": 1000, "width": 8000, "height": 8000},
            "motion": {
                "kind": "LINEAR",
                "end_crop": {"x": 2000, "y": 1000, "width": 8000, "height": 8000},
            },
        }
    )
    plan = RenderPlan(
        output=profile(),
        duration_ms=2000,
        clips=(
            clip(red, fade_out_ms=125),
            clip(blue, 500, fade_in_ms=125),
            clip(split, 1000, visual=cropped),
            clip(gradient, 1500, visual=motion),
        ),
    )
    progress = []
    movie = execute_media(
        "render", "timeline", plan.model_dump_json().encode(), observer=progress.append
    )
    with av.open(BytesIO(movie)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height, stream.average_rate) == (256, 256, 24)
        assert stream.codec_context.name == "h264"
        assert stream.codec_context.pix_fmt == "yuv420p"
        frames = [frame.to_image().getpixel((128, 128)) for frame in container.decode(video=0)]
        assert len(frames) == 48 and float(stream.duration * stream.time_base) == 2.0
    assert frames[0][0] > 210 and frames[0][2] < 30
    assert frames[11][0] < 100
    assert max(frames[12]) < 10 and frames[15][2] > 210
    assert frames[24][1] > 210 and frames[24][0] < 30
    assert frames[-1][1] - frames[36][1] > 15
    assert frames[36][0] - frames[-1][0] > 15
    with av.open(BytesIO(movie)) as container:
        audio = container.streams.audio[0]
        assert audio.codec_context.name == "aac" and audio.sample_rate == 48000
        assert audio.layout.name == "stereo"
        decoded = list(container.decode(audio=0))
        assert sum(frame.samples for frame in decoded) >= 96000
        assert all(frame.format.name == "fltp" for frame in decoded)
        assert all(
            not any(bytes(plane)[: frame.samples * 4])
            for frame in decoded
            for plane in frame.planes
        )
    assert [value for value in progress if value is not None][-1] == 100


@pytest.mark.parametrize(
    "mutation",
    [
        {"output_path": "../../private/output.mp4"},
        {"filter_complex": "movie=http://127.0.0.1/secret"},
        {"command": "ffmpeg; whoami"},
        {"clips": []},
        {"duration_ms": True},
        {"duration_ms": 1000},
        {"clips.0.start_ms": 1},
        {"clips.0.fade_out_ms": 501},
        {"clips.0.visual.motion.kind": "movie=/private/key"},
        {"clips.0.visual.crop.x": -1},
        {"output.video_encoder": "external"},
        {"output.audio_sample_rate": 44100},
        {"output.fps": 120, "duration_ms": 3_600_000},
        {"output.width": 3840, "output.height": 2160, "duration_ms": 60_000},
    ],
)
def test_plan_rejects_untrusted_expressions_paths_ranges_and_workloads(mutation):
    body = RenderPlan(
        output=profile(), duration_ms=500, clips=(clip(Image.new("RGB", (2, 2))),)
    ).model_dump(mode="json")
    for path, value in mutation.items():
        parts = path.split(".")
        target = body
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[parts[-1]] = value
    with pytest.raises(ValidationError):
        RenderPlan.model_validate(body)


@pytest.mark.parametrize("image", ["file:///private/input.png", "http://127.0.0.1/secret", "!!!!"])
def test_child_refuses_image_locations_and_invalid_base64(image):
    body = RenderPlan(
        output=profile(), duration_ms=500, clips=(clip(Image.new("RGB", (2, 2))),)
    ).model_dump(mode="json")
    body["clips"][0]["image"] = image
    with pytest.raises(MediaExecutionError) as result:
        execute_media("render", "timeline", json.dumps(body).encode())
    assert result.value.reason == "INVALID_MEDIA"


def test_encoded_output_has_no_fixed_byte_ceiling():
    plan = RenderPlan(
        output=profile(),
        duration_ms=500,
        clips=(clip(Image.new("RGB", (100, 100), (230, 20, 10))),),
    )
    assert command_for("render", "timeline").output_limit is None
    movie = execute_media("render", "timeline", plan.model_dump_json().encode())
    assert len(movie) > 10
    with av.open(BytesIO(movie)) as container:
        assert len(list(container.decode(video=0))) == 12
