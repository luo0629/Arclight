"""Internal child entry point. No project data, names, URLs or credentials."""

import json
import sys
import zlib
from io import BytesIO

from arclight.media.limits import restrict_process


def check_jpeg(data: bytes) -> None:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("Not JPEG")
    offset = 2
    while offset < len(data):
        if data[offset] != 255:
            raise ValueError("Invalid JPEG marker")
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            if offset != len(data):
                raise ValueError("Trailing JPEG data")
            return
        size = int.from_bytes(data[offset : offset + 2], "big")
        if marker in {0, 0xD8} or size < 2 or offset + size > len(data):
            raise ValueError("Invalid JPEG segment")
        offset += size
        if marker == 0xDA:
            while offset < len(data):
                if data[offset] != 255:
                    offset += 1
                    continue
                following = offset + 1
                while following < len(data) and data[following] == 255:
                    following += 1
                if following >= len(data):
                    break
                if data[following] == 0 or 0xD0 <= data[following] <= 0xD7:
                    offset = following + 1
                else:
                    break
    raise ValueError("Incomplete JPEG")


def inspect_image(data: bytes, media_type: str) -> dict:
    import warnings

    from PIL import Image, PngImagePlugin

    if media_type == "image/png":
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Invalid PNG signature")
        offset, ended = 8, False
        while offset < len(data):
            size = int.from_bytes(data[offset : offset + 4], "big")
            end = offset + 12 + size
            if end > len(data) or ended:
                raise ValueError("Truncated PNG or trailing data")
            tag = data[offset + 4 : offset + 8]
            expected = int.from_bytes(data[end - 4 : end], "big")
            if zlib.crc32(data[offset + 4 : end - 4]) != expected:
                raise ValueError("PNG checksum mismatch")
            ended = tag == b"IEND" and size == 0
            offset = end
        if not ended:
            raise ValueError("Incomplete PNG")
        PngImagePlugin.MAX_TEXT_CHUNK = 256 * 1024
        PngImagePlugin.MAX_TEXT_MEMORY = 512 * 1024
    else:
        check_jpeg(data)

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
            actual = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format or "")
            width, height = image.size
            if (
                actual != media_type
                or width * height > 4_000_000
                or max(width, height) > 4096
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Invalid image format or dimensions")
            image.verify()
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image:
            image.load()
    return {"media_type": media_type, "width": width, "height": height}


def inspect_text(data: bytes, media_type: str) -> dict:
    text = data.decode("utf-8-sig")
    if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ValueError("Invalid plain text")
    if max(map(len, text.splitlines()), default=0) > 20_000:
        raise ValueError("Line too long")
    return {"media_type": media_type, "characters": len(text), "encoding": "utf-8"}


def main() -> None:
    restrict_process()
    media_type = sys.argv[1]
    data = sys.stdin.buffer.read(32 * 1024**2 + 1)
    if not 0 < len(data) <= 32 * 1024**2:
        raise ValueError("Invalid input size")
    if media_type in {"image/png", "image/jpeg"}:
        result = inspect_image(data, media_type)
    elif media_type in {"text/plain", "text/markdown"}:
        result = inspect_text(data, media_type)
    elif media_type in {"audio/wav", "audio/mpeg", "audio/ogg"}:
        from arclight.media.audio import inspect_audio

        result = inspect_audio(data, media_type)
    elif media_type in {"font/ttf", "font/otf"}:
        from arclight.media.fonts import inspect_font

        result = inspect_font(data, media_type)
    else:
        raise ValueError("Unsupported media")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
