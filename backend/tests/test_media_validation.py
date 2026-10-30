import base64
import hashlib
import struct
import subprocess
import sys
import wave
import zlib
from io import BytesIO

import av
import pytest
from PIL import Image
from test_asset_versions import image_bytes

from arclight.core.errors import DomainError
from arclight.domain.asset_versions import decode_image
from arclight.domain.file_policy import check_file_policy
from arclight.media.validation import validate_file


def wave_bytes(rate=8000, channels=1, seconds=1):
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(rate * seconds * channels * 2))
    return output.getvalue()


def compressed_audio(media_type):
    output = BytesIO()
    codec, format_name, samples = (
        ("libmp3lame", "mp3", 1152) if media_type == "audio/mpeg" else ("libopus", "ogg", 960)
    )
    with av.open(output, "w", format=format_name) as container:
        stream = container.add_stream(codec, rate=48000)
        stream.layout = "mono"
        for index in range(4):
            frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.sample_rate = 48000
            frame.pts = index * samples
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return output.getvalue()


def font_bytes(is_ttf=True):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=is_ttf)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({65: "A"})
    outlines = {}
    for name in [".notdef", "A"]:
        pen = TTGlyphPen(None) if is_ttf else T2CharStringPen(600, None)
        pen.moveTo((100, 0))
        pen.lineTo((300, 600))
        pen.lineTo((500, 0))
        pen.closePath()
        outlines[name] = pen.glyph() if is_ttf else pen.getCharString()
    if is_ttf:
        builder.setupGlyf(outlines)
    else:
        builder.setupCFF("TestFont", {"FullName": "Test Font"}, outlines, {})
    builder.setupHorizontalMetrics({name: (600, 0) for name in outlines})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Test Font", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


@pytest.mark.parametrize(
    "media_type,name,purpose",
    [
        ("image/png", "image.PNG", "REFERENCE_IMAGE"),
        ("image/jpeg", "image.jpeg", "REFERENCE_IMAGE"),
        ("text/plain", "story.txt", "STORY"),
        ("text/markdown", "story.md", "STORY"),
        ("audio/wav", "voice.wav", "AUDIO"),
        ("audio/mpeg", "music.mp3", "MUSIC"),
        ("audio/ogg", "voice.opus", "AUDIO"),
        ("font/ttf", "font.ttf", "FONT"),
        ("font/otf", "font.otf", "FONT"),
    ],
)
def test_all_supported_files_are_fully_parsed(media_type, name, purpose):
    if media_type.startswith("image/"):
        output = BytesIO()
        Image.new("RGB", (10, 8)).save(
            output, format="PNG" if media_type == "image/png" else "JPEG"
        )
        data = output.getvalue()
    elif media_type.startswith("text/"):
        data = "\ufeff# 测试故事\n雨夜。".encode()
    elif media_type == "audio/wav":
        data = wave_bytes()
    elif media_type.startswith("audio/"):
        data = compressed_audio(media_type)
    else:
        data = font_bytes(media_type == "font/ttf")
    digest = hashlib.sha256(data).hexdigest()
    result = validate_file(data, purpose, name, media_type, digest)
    assert result.data == data and result.checksum == digest and result.media_type == media_type
    assert result.details["media_type"] == media_type
    if media_type.startswith("audio/"):
        assert 0 < result.details["duration_ms"] <= 1000
    if media_type.startswith("font/"):
        assert result.details["glyphs"] == 2


@pytest.mark.parametrize(
    "purpose,name,media_type,size",
    [
        ("FONT", "font.png", "image/png", 10),
        ("REFERENCE_IMAGE", "file.exe", "image/png", 10),
        ("REFERENCE_IMAGE", "file.jpg", "image/png", 10),
        ("STORY", "file.docx", "text/plain", 10),
        ("STORY", "file.txt", "text/plain", 256 * 1024 + 1),
        ("REFERENCE_IMAGE", "file.png", "image/png", 1024**2 + 1),
        ("FONT", "font.ttf", "font/ttf", 8 * 1024**2 + 1),
        ("MUSIC", "music.wav", "audio/wav", 32 * 1024**2 + 1),
        ("STORY", "..\\file.txt", "text/plain", 10),
        ("STORY", "file.txt:payload.txt", "text/plain", 10),
        ("STORY", "file.txt\x00", "text/plain", 10),
    ],
)
def test_policy_rejects_forgery_paths_and_excess_before_read(purpose, name, media_type, size):
    with pytest.raises(DomainError):
        check_file_policy(purpose, name, media_type, size)


@pytest.mark.parametrize(
    "media_type,name,purpose,data",
    [
        ("image/png", "fake.png", "REFERENCE_IMAGE", b"GIF89a"),
        ("image/png", "fake.png", "REFERENCE_IMAGE", b"PK\x03\x04zip archive"),
        ("image/png", "fake.png", "REFERENCE_IMAGE", image_bytes()[:-1]),
        ("image/png", "fake.png", "REFERENCE_IMAGE", image_bytes() + b"<script>bad</script>"),
        ("text/plain", "fake.txt", "STORY", b"hello\x00binary"),
        ("text/plain", "fake.txt", "STORY", b"\xff\xfe\x00bad"),
        ("text/markdown", "fake.md", "STORY", b" \n\t"),
        ("audio/wav", "fake.wav", "AUDIO", wave_bytes()[:-1]),
        ("audio/wav", "fake.wav", "AUDIO", wave_bytes() + b"junk"),
        ("audio/wav", "fake.wav", "AUDIO", b"#EXTM3U\nhttp://127.0.0.1/private"),
        ("font/ttf", "fake.ttf", "FONT", b"wOFF" + bytes(100)),
        ("font/ttf", "fake.ttf", "FONT", b"ttcf" + bytes(100)),
    ],
    ids=lambda value: (
        "bytes-" + hashlib.sha256(value).hexdigest()[:8] if isinstance(value, bytes) else None
    ),
)
def test_invalid_truncated_trailing_binary_and_external_files(media_type, name, purpose, data):
    with pytest.raises(DomainError):
        validate_file(data, purpose, name, media_type)


@pytest.mark.parametrize(
    "media_type,name", [("audio/mpeg", "voice.mp3"), ("audio/ogg", "voice.ogg")]
)
def test_compressed_audio_integrity_and_actual_format(media_type, name):
    data = compressed_audio(media_type)
    for invalid in (data[:-8], data + b"junk", wave_bytes()):
        with pytest.raises(DomainError):
            validate_file(invalid, "AUDIO", name, media_type)


def test_font_checksum_and_directory_integrity():
    data = font_bytes()
    for invalid in (data[:-10], data + b"junk", data[:100] + bytes([data[100] ^ 1]) + data[101:]):
        with pytest.raises(DomainError):
            validate_file(invalid, "FONT", "font.ttf", "font/ttf")


def test_cyclic_composite_font_is_rejected():
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphComponent

    font = TTFont(BytesIO(font_bytes()), recalcBBoxes=False)
    component = GlyphComponent()
    component.glyphName, component.x, component.y, component.flags = "A", 0, 0, 4
    glyph = Glyph()
    glyph.numberOfContours = -1
    glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax = 0, 0, 600, 600
    glyph.components = [component]
    font["glyf"]["A"] = glyph
    output = BytesIO()
    font.save(output)
    with pytest.raises(DomainError):
        validate_file(output.getvalue(), "FONT", "cycle.ttf", "font/ttf")


def test_progressive_jpeg_decodes_and_hidden_tail_is_rejected():
    output = BytesIO()
    Image.new("RGB", (16, 16)).save(output, format="JPEG", progressive=True)
    data = output.getvalue()
    assert validate_file(data, "REFERENCE_IMAGE", "test.jpg", "image/jpeg").data == data
    for invalid in (data + b"payload\xff\xd9", data[:-3]):
        with pytest.raises(DomainError):
            validate_file(invalid, "REFERENCE_IMAGE", "test.jpg", "image/jpeg")


def test_wave_partial_sample_is_rejected_even_with_matching_container_length():
    data = bytearray(wave_bytes())
    data.pop()
    data[4:8] = (len(data) - 8).to_bytes(4, "little")
    data[40:44] = (len(data) - 44).to_bytes(4, "little")
    with pytest.raises(DomainError):
        validate_file(bytes(data), "AUDIO", "voice.wav", "audio/wav")


def test_image_pixel_and_compressed_metadata_bombs():
    for dimensions in ((2001, 2000), (4097, 1)):
        output = BytesIO()
        Image.new("RGB", dimensions).save(output, format="PNG")
        with pytest.raises(DomainError):
            decode_image(base64.b64encode(output.getvalue()).decode(), "image/png")
    original = image_bytes()
    compressed = b"description\x00\x00" + zlib.compress(bytes(300 * 1024))
    chunk = b"zTXt" + compressed
    bomb = original[:33] + struct.pack(">I", len(compressed)) + chunk
    bomb += struct.pack(">I", zlib.crc32(chunk)) + original[33:]
    with pytest.raises(DomainError):
        validate_file(bomb, "REFERENCE_IMAGE", "bomb.png", "image/png")


def test_audio_duration_rate_and_channels_are_bounded():
    for data in (wave_bytes(seconds=601), wave_bytes(rate=96000), wave_bytes(channels=3)):
        with pytest.raises(DomainError):
            validate_file(data, "AUDIO", "voice.wav", "audio/wav")


def test_hash_mismatch_and_real_process_timeout(monkeypatch):
    from arclight.media import validation

    with pytest.raises(DomainError, match="哈希"):
        validate_file(b"text", "STORY", "story.txt", "text/plain", "0" * 64)
    monkeypatch.setattr(validation, "PARSER_TIMEOUT", 0.001)
    with pytest.raises(DomainError, match="安全解析"):
        validate_file(b"text", "STORY", "story.txt", "text/plain")


def test_os_rejects_excessive_parser_allocation():
    script = (
        "from arclight.media.limits import restrict_process; restrict_process(); "
        "data = bytearray(768 * 1024**2)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=10)
    assert result.returncode != 0 and b"MemoryError" in result.stderr


def test_capability_is_not_issued_for_an_inconsistent_declaration(client, episode):
    from sqlalchemy import select

    from arclight.persistence.models import UploadIntent

    endpoint = f"/api/v1/projects/{episode['project_id']}/uploads"
    response = client.post(
        endpoint,
        json={
            "purpose": "FONT",
            "original_name": "image.png",
            "media_type": "image/png",
            "byte_size": 20,
            "checksum": "0" * 64,
        },
    )
    assert response.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(UploadIntent)) is None
