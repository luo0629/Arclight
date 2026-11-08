"""Private bounded result downloads, separate from normalized Provider receipts."""

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from PIL import Image

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import MAX_IMAGE_BYTES
from arclight.providers.media_submission import MediaSubmission


@dataclass(frozen=True)
class MediaOutput:
    data: bytes
    media_type: str


class MediaOutputProvider(Protocol):
    name: str

    def download(self, request: MediaSubmission, handle: str, max_bytes: int) -> MediaOutput:
        """Return at most max_bytes; real adapters must bound their network reads."""
        ...


class MockMediaOutputProvider:
    name = "mock"

    def download(self, request: MediaSubmission, handle: str, max_bytes: int) -> MediaOutput:
        spec = request.snapshot["context"]["generation"]
        allowed = {
            f"mock/image/{request.attempt_id}/{index}" for index in range(spec.get("batch_size", 1))
        }
        if handle not in allowed:
            raise DomainError("MEDIA_OUTPUT_UNAVAILABLE", "Mock result handle is unavailable", 503)
        output = spec["output_spec"]
        width, height = output.get("width", 960), output.get("height", 540)
        if width * height > 4_000_000 or max(width, height) > 4096:
            raise DomainError("INVALID_IMAGE", "Mock output exceeds image limits", 422)
        kind = "JPEG" if output.get("format") in {"jpeg", "jpg"} else "PNG"
        stream = BytesIO()
        # Different outputs remain distinct even with an identical frozen input.
        index = int(handle.rsplit("/", 1)[1])
        Image.new("RGB", (width, height), (30 + index * 3, 70, 120)).save(stream, kind)
        data = stream.getvalue()
        if len(data) > min(max_bytes, MAX_IMAGE_BYTES):
            raise DomainError("INVALID_IMAGE", "Mock output exceeds byte limit", 422)
        return MediaOutput(data, "image/jpeg" if kind == "JPEG" else "image/png")


def get_media_output_provider(
    provider: str = "mock", settings: Settings | None = None, *, connection=None
) -> MediaOutputProvider:
    if connection is not None and settings is not None:
        from arclight.providers.native_media import NativeMediaProvider

        return NativeMediaProvider(settings, connection)
    if provider == "openai" and settings is not None:
        from arclight.providers.openai_images import OpenAIImageProvider

        return OpenAIImageProvider(settings)
    if provider != "mock":
        raise DomainError("PROVIDER_NOT_INSTALLED", "图像结果适配器不可用", 422)
    return MockMediaOutputProvider()
