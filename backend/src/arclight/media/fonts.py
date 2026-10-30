"""Accept standalone SFNT fonts only; reject containers, overlap and corruption."""

from io import BytesIO
from struct import unpack_from


def inspect_font(data: bytes, media_type: str) -> dict:
    from fontTools.ttLib import TTFont

    signature = b"\x00\x01\x00\x00" if media_type == "font/ttf" else b"OTTO"
    if data[:4] != signature or len(data) < 12:
        raise ValueError("Not a standalone SFNT font")
    count = unpack_from(">H", data, 4)[0]
    directory_end = 12 + count * 16
    if not 1 <= count <= 128 or directory_end > len(data):
        raise ValueError("Invalid SFNT table directory")
    tables, ranges = set(), []
    for index in range(count):
        tag, checksum, offset, size = unpack_from(">4sIII", data, 12 + index * 16)
        if tag in tables or offset < directory_end or offset % 4 or offset + size > len(data):
            raise ValueError("Invalid or overlapping font table")
        tables.add(tag)
        if size:
            ranges.append((offset, offset + size))
        table = bytearray(data[offset : offset + size])
        if tag == b"head":
            if size < 54:
                raise ValueError("Truncated head table")
            table[8:12] = bytes(4)
        table.extend(bytes((-len(table)) % 4))
        actual = sum(unpack_from(">I", table, pos)[0] for pos in range(0, len(table), 4))
        if actual & 0xFFFFFFFF != checksum:
            raise ValueError("Font table checksum mismatch")
    ranges.sort()
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:], strict=False)):
        raise ValueError("Overlapping font tables")
    if not ranges or len(data) != (ranges[-1][1] + 3) & ~3:
        raise ValueError("Trailing or incomplete font data")
    padded = data + bytes((-len(data)) % 4)
    if (
        sum(unpack_from(">I", padded, pos)[0] for pos in range(0, len(padded), 4)) & 0xFFFFFFFF
        != 0xB1B0AFBA
    ):
        raise ValueError("Font checksum adjustment mismatch")
    required = {b"head", b"hhea", b"hmtx", b"maxp", b"cmap", b"name"}
    required |= {b"glyf", b"loca"} if media_type == "font/ttf" else {b"CFF "}
    if not required <= tables or any(tag in tables for tag in (b"SVG ", b"fvar", b"CFF2")):
        raise ValueError("Unsupported font tables")
    with TTFont(BytesIO(data), lazy=False, checkChecksums=2) as font:
        if not 1 <= font["maxp"].numGlyphs <= 20000:
            raise ValueError("Excessive glyph count")
        font.ensureDecompiled()
        glyphs = font.getGlyphSet()
        from fontTools.pens.recordingPen import DecomposingRecordingPen

        operations = 0
        for name in font.getGlyphOrder():
            pen = DecomposingRecordingPen(glyphs, skipMissingComponents=False)
            glyphs[name].draw(pen)
            operations += len(pen.value)
            if operations > 500_000:
                raise ValueError("Excessive font outline complexity")
    return {"media_type": media_type, "glyphs": len(glyphs)}
