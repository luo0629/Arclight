"""Credential-free bounded FFmpeg still-preview encoder and MP4 finalizer."""

import sys
from io import BytesIO

from arclight.media.limits import restrict_process


def main() -> None:
    restrict_process()
    import av
    from PIL import Image, ImageOps

    data = sys.stdin.buffer.read(1024 * 1024 + 1)
    if not 0 < len(data) <= 1024 * 1024:
        raise ValueError("Invalid render input size")
    output = BytesIO()
    if sys.argv[1] == "encode":
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as source:
            if source.width * source.height > 4_000_000 or max(source.size) > 4096:
                raise ValueError("Invalid image dimensions")
            image = ImageOps.pad(source.convert("RGB"), (256, 256))
        with av.open(output, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=24)
            stream.width = stream.height = 256
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "threads": "1"}
            for index in range(24):
                frame = av.VideoFrame.from_image(image)
                frame.pts = index
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    elif sys.argv[1] == "finalize":
        with (
            av.open(BytesIO(data), mode="r") as source,
            av.open(output, mode="w", format="mp4") as target,
        ):
            incoming = source.streams.video[0]
            outgoing = target.add_stream_from_template(incoming)
            for packet in source.demux(incoming):
                if packet.dts is not None:
                    packet.stream = outgoing
                    target.mux(packet)
    else:
        raise ValueError("Unknown rendering phase")
    result = output.getvalue()
    if not 0 < len(result) <= 1024 * 1024:
        raise ValueError("Invalid render output size")
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    main()
