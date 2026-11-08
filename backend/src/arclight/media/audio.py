"""Check container boundaries, then decode every sample with restricted demuxers."""

from fractions import Fraction
from io import BytesIO

MAX_AUDIO_SECONDS = 600


def check_wave(data: bytes) -> None:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Not RIFF/WAVE")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ValueError("Truncated or trailing RIFF bytes")
    offset, chunks, seen = 12, 0, set()
    alignment, rate, data_size = 0, 0, 0
    while offset < len(data):
        tag = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if offset + 8 > len(data) or offset + 8 + size > len(data) or chunks >= 128:
            raise ValueError("Invalid RIFF chunk")
        if tag in {b"fmt ", b"data"}:
            if tag in seen or size == 0:
                raise ValueError("Invalid WAVE chunks")
            seen.add(tag)
            if tag == b"fmt ":
                if size < 16:
                    raise ValueError("Incomplete WAVE format")
                info = data[offset + 8 : offset + 8 + size]
                channels = int.from_bytes(info[2:4], "little")
                rate = int.from_bytes(info[4:8], "little")
                alignment = int.from_bytes(info[12:14], "little")
                bits = int.from_bytes(info[14:16], "little")
                if (
                    not 1 <= channels <= 2
                    or not 8000 <= rate <= 48000
                    or bits not in {8, 16, 24, 32}
                    or alignment != channels * (bits // 8)
                ):
                    raise ValueError("Unsupported WAVE sample format")
            else:
                data_size = size
        offset += 8 + size + (size % 2)
        chunks += 1
    if offset != len(data) or seen != {b"fmt ", b"data"}:
        raise ValueError("Incomplete WAVE file")
    if not alignment or data_size % alignment or data_size // alignment > rate * MAX_AUDIO_SECONDS:
        raise ValueError("Incomplete or excessive WAVE samples")


def check_mp3(data: bytes) -> None:
    offset, end, frames = 0, len(data), 0
    if data[:3] == b"ID3":
        if len(data) < 10 or data[3] not in {2, 3, 4} or any(b & 0x80 for b in data[6:10]):
            raise ValueError("Invalid ID3")
        size = sum(value << shift for value, shift in zip(data[6:10], (21, 14, 7, 0), strict=True))
        offset = 10 + size + (10 if data[3] == 4 and data[5] & 0x10 else 0)
        if offset > 1024**2:
            raise ValueError("Excessive ID3 metadata")
    if data[-128:-125] == b"TAG":
        end -= 128
    while offset < end:
        header = int.from_bytes(data[offset : offset + 4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        if (
            header >> 21 != 0x7FF
            or version == 1
            or layer != 1
            or bitrate_index in {0, 15}
            or rate_index == 3
        ):
            raise ValueError("Invalid MP3 frame")
        rates = (44100, 48000, 32000)
        rate = rates[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
        bitrates = (
            (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
            if version == 3
            else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        )
        size = (144000 if version == 3 else 72000) * bitrates[bitrate_index] // rate
        size += (header >> 9) & 1
        if size < 4 or offset + size > end or frames > 30000:
            raise ValueError("Incomplete MP3 frame")
        offset += size
        frames += 1
    if offset != end or not frames:
        raise ValueError("Empty MP3")


def check_ogg(data: bytes) -> None:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(crc)
    offset, sequence, serial, ended = 0, 0, None, False
    while offset < len(data):
        page_view = memoryview(data)[offset:]
        if len(page_view) < 27 or page_view[:5] != b"OggS\x00" or ended:
            raise ValueError("Invalid Ogg page")
        count = page_view[26]
        if len(page_view) < 27 + count:
            raise ValueError("Truncated Ogg lacing")
        size = 27 + count + sum(page_view[27 : 27 + count])
        if size > len(page_view) or int.from_bytes(page_view[18:22], "little") != sequence:
            raise ValueError("Truncated or unordered Ogg")
        page = bytearray(page_view[:size])
        current_serial = bytes(page[14:18])
        if serial is None:
            serial = current_serial
            if not page[5] & 2:
                raise ValueError("Missing Ogg start")
        elif current_serial != serial or page[5] & 2:
            raise ValueError("Multiplexed or chained Ogg is unsupported")
        expected = int.from_bytes(page[22:26], "little")
        page[22:26] = bytes(4)
        crc = 0
        for byte in page:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) ^ byte) & 255]
        if crc != expected:
            raise ValueError("Ogg checksum mismatch")
        ended = bool(page[5] & 4)
        offset += size
        sequence += 1
    if not ended:
        raise ValueError("Incomplete Ogg stream")


def inspect_audio(data: bytes, media_type: str) -> dict:
    import av

    formats = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/flac": "flac"}
    expected_samples = None
    if media_type == "audio/flac":
        if (
            len(data) < 42
            or data[:5] not in {b"fLaC\x00", b"fLaC\x80"}
            or data[5:8] != b"\x00\x00\x22"
        ):
            raise ValueError("Invalid FLAC STREAMINFO")
        expected_samples = int.from_bytes(data[18:26], "big") & ((1 << 36) - 1)
        if not 0 < expected_samples <= 48000 * MAX_AUDIO_SECONDS:
            raise ValueError("Unknown or excessive FLAC sample count")
    else:
        {"audio/wav": check_wave, "audio/mpeg": check_mp3, "audio/ogg": check_ogg}[media_type](data)

    def refuse_external(*_args, **_kwargs):
        raise ValueError("External resources are forbidden")

    with av.open(
        BytesIO(data),
        format=formats[media_type],
        options={"protocol_whitelist": "file,pipe", "err_detect": "explode"},
        io_open=refuse_external,
    ) as container:
        if not isinstance(container, av.container.InputContainer):
            raise ValueError("Input container required")
        if len(container.streams) != 1 or len(container.streams.audio) != 1:
            raise ValueError("Exactly one audio stream is required")
        stream = container.streams.audio[0]
        allowed = {
            "audio/wav": {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"},
            "audio/mpeg": {"mp3", "mp3float"},
            "audio/ogg": {"vorbis", "opus"},
            "audio/flac": {"flac"},
        }
        if stream.codec_context.name not in allowed[media_type]:
            raise ValueError("Unsupported audio codec")
        stream.codec_context.thread_count = 1
        elapsed, samples = Fraction(0), 0
        rate, channels = 0, 0
        for frames, frame in enumerate(container.decode(stream), 1):
            rate, channels = frame.sample_rate, len(frame.layout.channels)
            if not 8000 <= rate <= 48000 or not 1 <= channels <= 2:
                raise ValueError("Unsupported audio sample rate or channels")
            elapsed += Fraction(frame.samples, rate)
            samples += frame.samples
            if elapsed > MAX_AUDIO_SECONDS or frames > 30000 or samples > 48_000 * 600:
                raise ValueError("Audio exceeds decoding limit")
        if samples == 0:
            raise ValueError("No decoded audio")
        if expected_samples is not None and samples != expected_samples:
            raise ValueError("Incomplete FLAC samples")
    return {
        "media_type": media_type,
        "duration_ms": int(elapsed * 1000),
        "sample_rate": rate,
        "channels": channels,
    }
