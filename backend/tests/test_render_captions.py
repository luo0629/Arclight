"""Decode actual frames to verify Unicode captions, styles and half-open cue windows."""

import base64
from fractions import Fraction
from io import BytesIO

import av
import pytest
from PIL import Image
from pydantic import ValidationError
from test_render_mixing import audio_plan

from arclight.domain.render_manifests import SubtitleStyle
from arclight.media.execution import execute_media
from arclight.media.render_plan import RenderCue, RenderSubtitles
from arclight.media.timeline_captions import SubtitleBurner
from arclight.media.timeline_encoder import encode_timeline


def caption_font(characters="AB中文配音审核后的字幕编辑显示内容空格"):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    characters = " " + "".join(sorted(set(characters) - {" "}))
    names = [".notdef", *(f"u{ord(char):04X}" for char in characters)]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap(
        {ord(char): name for char, name in zip(characters, names[1:], strict=True)}
    )
    outlines = {}
    for index, name in enumerate(names):
        pen = TTGlyphPen(None)
        if name != "u0020":
            pen.moveTo((100, 0))
            pen.lineTo((300 + index % 3 * 80, 600))
            pen.lineTo((500, 0))
            pen.closePath()
        outlines[name] = pen.glyph()
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({name: (600, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Arclight Test Caption", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


def caption_plan(cues=None, **style):
    subtitles = RenderSubtitles(
        font=base64.b64encode(caption_font()).decode(),
        font_media_type="font/ttf",
        style=SubtitleStyle(**style),
        cues=tuple(cues or [RenderCue(text="A", start_ms=0, duration_ms=1000)]),
    )
    return audio_plan([], [], subtitles=subtitles)


def bright_bounds(image):
    return image.convert("L").point(lambda value: 255 if value > 25 else 0).getbbox()


def test_sealed_unicode_caption_pixels_follow_exact_cue_start_end_and_empty_gaps():
    plan = caption_plan(
        [
            RenderCue(text="中文\n配音", start_ms=200, duration_ms=200),
            RenderCue(text="A B", start_ms=600, duration_ms=200),
        ],
        font_size=32,
        margin_bottom=20,
    )
    movie = execute_media("render", "timeline", plan.model_dump_json().encode())
    with av.open(BytesIO(movie)) as container:
        observations = [bright_bounds(frame.to_image()) for frame in container.decode(video=0)]
    assert len(observations) == 25
    assert [index for index, box in enumerate(observations) if box is not None] == [
        *range(5, 10),
        *range(15, 20),
    ]
    assert observations[5][3] <= 239


@pytest.mark.parametrize(
    "alignment,expected",
    [("BOTTOM_LEFT", "left"), ("BOTTOM_CENTER", "center"), ("BOTTOM_RIGHT", "right")],
)
def test_caption_alignment_colors_outline_margin_and_bold_are_actually_drawn(alignment, expected):
    plan = caption_plan(
        alignment=alignment,
        color="#FF0000",
        outline_color="#0000FF",
        outline_width=3,
        margin_bottom=31,
        font_size=36,
        bold=True,
    )
    with SubtitleBurner(plan) as burner, Image.new("RGB", (256, 256)) as image:
        burner.apply(image, Fraction(0))
        box = bright_bounds(image)
        assert box is not None and box[3] <= 226 and box[3] >= 222
        if expected == "left":
            assert box[0] < 12
        elif expected == "right":
            assert box[2] > 244
        else:
            assert abs((box[0] + box[2]) / 2 - 128) < 3
        pixels = [image.getpixel((x, y)) for y in range(256) for x in range(256)]
        assert any(red > 200 and blue < 30 for red, _, blue in pixels)
        assert any(blue > 200 and red < 30 for red, _, blue in pixels)
    regular = caption_plan(font_size=36, outline_width=0, bold=False)
    bold = caption_plan(font_size=36, outline_width=0, bold=True)
    counts = []
    for item in (regular, bold):
        with SubtitleBurner(item) as burner, Image.new("RGB", (256, 256)) as image:
            burner.apply(image, Fraction(0))
            counts.append(
                sum(max(image.getpixel((x, y))) > 30 for y in range(256) for x in range(256))
            )
    assert counts[1] > counts[0]


def test_long_unicode_caption_wraps_without_truncation_and_keeps_explicit_newline():
    text = "中文配音字幕" * 3 + "\n编辑显示内容"
    plan = caption_plan(
        [RenderCue(text=text, start_ms=0, duration_ms=1000)], font_size=25, margin_bottom=10
    )
    with SubtitleBurner(plan) as burner, Image.new("RGB", (256, 256)) as image:
        wrapped, _ = burner.layouts[0]
        assert wrapped.replace("\n", "") == text.replace("\n", "")
        assert wrapped.count("\n") >= 2 and "\n编辑显示内容" in wrapped
        burner.apply(image, Fraction(0))
        box = bright_bounds(image)
        assert box[0] >= 0 and box[2] <= 256 and box[1] > 0
        assert box[3] - box[1] > 50


@pytest.mark.parametrize(
    "text,style,match",
    [
        ("缺字", {}, "missing required glyphs"),
        ("中文" * 1000, {"font_size": 144}, "do not fit"),
        ("A", {"margin_bottom": 500}, "do not fit"),
    ],
)
def test_missing_glyphs_or_frame_overflow_fail_without_dropping_caption_content(text, style, match):
    plan = caption_plan([RenderCue(text=text, start_ms=0, duration_ms=1000)], **style)
    with pytest.raises(ValueError, match=match):
        encode_timeline(plan.model_dump_json().encode())


@pytest.mark.parametrize(
    "cues",
    [
        [(500, 300), (200, 100)],
        [(100, 400), (499, 100)],
        [(900, 101)],
    ],
)
def test_subtitle_order_overlap_and_timeline_bounds_are_rejected(cues):
    with pytest.raises(ValidationError, match="subtitle order, overlap or range"):
        caption_plan(
            [RenderCue(text="A", start_ms=start, duration_ms=duration) for start, duration in cues]
        )


@pytest.mark.parametrize("text", ["", " ", "A\x00", "A\rB"])
def test_caption_control_characters_and_blank_content_are_rejected(text):
    with pytest.raises(ValidationError):
        RenderCue(text=text, start_ms=0, duration_ms=100)


def test_font_paths_and_extra_renderer_fields_are_rejected():
    plan = caption_plan()
    body = plan.model_dump(mode="json")
    body["subtitles"]["font"] = "C:/Windows/Fonts/private.ttf"
    with pytest.raises(ValueError):
        encode_timeline(__import__("json").dumps(body).encode())
    body["subtitles"]["path"] = "/tmp/caption.srt"
    with pytest.raises(ValidationError):
        type(plan).model_validate(body)
