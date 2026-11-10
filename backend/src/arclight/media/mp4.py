"""One byte-only MP4 opening policy for uploads and encoded render candidates."""

from io import BytesIO


def open_mp4(data: bytes):
    import av

    offset, boxes = 0, 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Incomplete MP4 box")
        size = int.from_bytes(data[offset : offset + 4], "big")
        header = 8
        if size == 1:
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data) or boxes > 10000:
            raise ValueError("Invalid MP4 boundaries")
        offset += size
        boxes += 1
    if data[4:8] != b"ftyp":
        raise ValueError("MP4 signature required")

    def refuse_external(*_args, **_kwargs):
        raise ValueError("External resources are forbidden")

    return av.open(
        BytesIO(data),
        format="mov",
        options={
            "err_detect": "explode",
            "protocol_whitelist": "pipe",
            "enable_drefs": "0",
            "use_absolute_path": "0",
        },
        io_open=refuse_external,
    )
