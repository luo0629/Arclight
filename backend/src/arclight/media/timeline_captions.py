"""Burn exact caption text with the frozen font; fail on missing glyphs or overflow."""

from __future__ import annotations

import base64
import unicodedata
from fractions import Fraction
from io import BytesIO
from typing import TYPE_CHECKING

from arclight.media.fonts import inspect_font
from arclight.media.render_plan import RenderPlan

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class SubtitleBurner:
    def __init__(self, plan: RenderPlan):
        from fontTools.ttLib import TTFont
        from PIL import Image, ImageDraw, ImageFont, features

        self.subtitles = plan.subtitles
        self.overlay: PILImage | None = None
        self.cached_index = -1
        self.layouts: list[tuple[str, tuple[int, int, int, int]]] = []
        if self.subtitles is None:
            return
        data = base64.b64decode(self.subtitles.font, validate=True)
        inspect_font(data, self.subtitles.font_media_type)
        with TTFont(BytesIO(data)) as font:
            supported = set((font.getBestCmap() or {}).keys())
        self.style = self.subtitles.style
        self.font = ImageFont.truetype(BytesIO(data), self.style.font_size)
        self.padding = max(2, plan.output.width // 50)
        self.size = (plan.output.width, plan.output.height)
        self.bold = max(1, self.style.font_size // 24) if self.style.bold else 0
        self.stroke = self.style.outline_width + self.bold
        self.align = {"BOTTOM_LEFT": "left", "BOTTOM_CENTER": "center", "BOTTOM_RIGHT": "right"}[
            self.style.alignment
        ]
        self.spacing = max(2, self.style.font_size // 5)
        with Image.new("L", (1, 1)) as measure:
            draw = ImageDraw.Draw(measure)
            for cue in self.subtitles.cues:
                if any(ord(char) not in supported for char in cue.text if not char.isspace()):
                    raise ValueError("Frozen subtitle font is missing required glyphs")
                if not features.check_feature("raqm") and any(
                    unicodedata.bidirectional(char) in {"R", "AL", "AN"} for char in cue.text
                ):
                    raise ValueError("Subtitle script requires a shaping-enabled renderer")
                text = self.wrap(cue.text.expandtabs(4))
                left, top, right, bottom = draw.multiline_textbbox(
                    (0, 0),
                    text,
                    font=self.font,
                    stroke_width=self.stroke,
                    spacing=self.spacing,
                    align=self.align,
                )
                box = (int(left), int(top), int(right), int(bottom))
                width, height = box[2] - box[0], box[3] - box[1]
                if (
                    width <= 0
                    or height <= 0
                    or width > self.size[0] - 2 * self.padding
                    or height + self.style.margin_bottom + self.padding > self.size[1]
                ):
                    raise ValueError("Subtitle text and style do not fit the output frame")
                self.layouts.append((text, box))

    def wrap(self, text: str) -> str:
        available = self.size[0] - 2 * (self.padding + self.stroke)
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            clusters: list[str] = []
            for char in paragraph:
                if clusters and unicodedata.category(char).startswith("M"):
                    clusters[-1] += char
                else:
                    clusters.append(char)
            for cluster in clusters:
                if self.font.getlength(cluster) > available:
                    raise ValueError("Subtitle glyph does not fit the output frame")
                if line and self.font.getlength(line + cluster) > available:
                    lines.append(line)
                    line = ""
                line += cluster
            lines.append(line)
        return "\n".join(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self.overlay is not None:
            self.overlay.close()

    def apply(self, image, stamp: Fraction) -> None:
        from PIL import Image, ImageDraw

        if self.subtitles is None:
            return
        index = next(
            (
                i
                for i, cue in enumerate(self.subtitles.cues)
                if cue.start_ms <= stamp < cue.start_ms + cue.duration_ms
            ),
            None,
        )
        if index is None:
            return
        if index != self.cached_index:
            if self.overlay is not None:
                self.overlay.close()
            text, box = self.layouts[index]
            width, height = box[2] - box[0], box[3] - box[1]
            self.overlay = Image.new("RGBA", (width, height))
            draw = ImageDraw.Draw(self.overlay)
            draw.multiline_text(
                (-box[0], -box[1]),
                text,
                fill=self.style.color,
                stroke_width=self.stroke,
                stroke_fill=self.style.outline_color,
                font=self.font,
                spacing=self.spacing,
                align=self.align,
            )
            if self.bold:
                draw.multiline_text(
                    (-box[0], -box[1]),
                    text,
                    fill=self.style.color,
                    stroke_width=self.bold,
                    stroke_fill=self.style.color,
                    font=self.font,
                    spacing=self.spacing,
                    align=self.align,
                )
            ink = self.overlay.getbbox()
            if ink is None:
                raise ValueError("Subtitle font produces no visible glyphs")
            trimmed = self.overlay.crop(ink)
            self.overlay.close()
            self.overlay = trimmed
            width, height = trimmed.size
            x = (
                self.padding
                if self.align == "left"
                else (
                    self.size[0] - self.padding - width
                    if self.align == "right"
                    else (self.size[0] - width) // 2
                )
            )
            self.position = (x, self.size[1] - self.style.margin_bottom - height)
            self.cached_index = index
        image.paste(self.overlay, self.position, self.overlay)
