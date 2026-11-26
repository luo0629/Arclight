"""Identity of the unmodified subtitle font distributed with the application."""

from hashlib import sha256
from importlib.resources import files

BUNDLED_FONT_NAME = "NotoSansSC-Regular.otf"
BUNDLED_FONT_TYPE = "font/otf"
BUNDLED_FONT_SIZE = 8_331_336
BUNDLED_FONT_SHA256 = "faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"
BUNDLED_FONT_SOURCE = (
    "Noto Sans SC Regular 2.004 (notofonts/noto-cjk, Sans2.004, "
    "523d033d6cb47f4a80c58a35753646f5c3608a78)"
)
BUNDLED_FONT_RIGHTS = "© 2014-2021 Adobe. SIL Open Font License 1.1. 许可文本随应用分发。"


def is_bundled_subtitle_font(data: bytes, media_type: str) -> bool:
    return (
        media_type == BUNDLED_FONT_TYPE
        and len(data) == BUNDLED_FONT_SIZE
        and sha256(data).hexdigest() == BUNDLED_FONT_SHA256
    )


def bundled_subtitle_font() -> bytes:
    data = files("arclight.media").joinpath("font_assets", BUNDLED_FONT_NAME).read_bytes()
    if not is_bundled_subtitle_font(data, BUNDLED_FONT_TYPE):
        raise ValueError("Bundled subtitle font does not match the published artifact")
    return data
