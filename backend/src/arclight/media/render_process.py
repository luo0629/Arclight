"""Credential-free bounded FFmpeg still-preview encoder and MP4 finalizer."""

from io import BytesIO


def render(data: bytes, phase: str) -> bytes:
    import av
    from PIL import Image, ImageOps

    if not data:
        raise ValueError("Invalid render input size")
    output = BytesIO()
    if phase == "encode":
        with Image.open(BytesIO(data), formats=["PNG", "JPEG"]) as image_source:
            if (
                image_source.width * image_source.height > 4_000_000
                or max(image_source.size) > 4096
            ):
                raise ValueError("Invalid image dimensions")
            image = ImageOps.pad(image_source.convert("RGB"), (256, 256))
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
    elif phase == "finalize":

        def refuse_external(*_args, **_kwargs):
            raise ValueError("External resources are forbidden")

        with (
            av.open(
                BytesIO(data),
                mode="r",
                format="mov",
                options={
                    "protocol_whitelist": "pipe",
                    "enable_drefs": "0",
                    "use_absolute_path": "0",
                },
                io_open=refuse_external,
            ) as source,
            av.open(output, mode="w", format="mp4") as target,
        ):
            if len(source.streams) != 1 or len(source.streams.video) != 1:
                raise ValueError("Exactly one preview video stream is required")
            incoming = source.streams.video[0]
            incoming.codec_context.thread_count = 1
            if incoming.codec_context.name != "h264":
                raise ValueError("Only the preview encoder's H264 output is accepted")
            outgoing = target.add_stream_from_template(incoming)
            for packet in source.demux(incoming):
                if packet.dts is not None:
                    packet.stream = outgoing
                    target.mux(packet)
    else:
        raise ValueError("Unknown rendering phase")
    result = output.getvalue()
    if not result:
        raise ValueError("Invalid render output size")
    return result
