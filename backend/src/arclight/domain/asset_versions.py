"""Bounded manual image imports and explicit version lifecycle transitions."""

import base64
import binascii
import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from arclight.core.errors import DomainError

MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_BASE64 = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_IMAGE_PIXELS = 4_000_000
TRANSITIONS = {
    "DRAFT": {"READY_FOR_REVIEW", "ARCHIVED"},
    "READY_FOR_REVIEW": {"APPROVED", "REJECTED", "ARCHIVED"},
    "APPROVED": {"STALE", "ARCHIVED"},
    "REJECTED": {"ARCHIVED"},
    "STALE": {"ARCHIVED"},
}


@dataclass(frozen=True)
class ImportedImage:
    data: bytes
    checksum: str
    media_type: str
    width: int
    height: int


def decode_image(encoded: str, media_type: str) -> ImportedImage:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DomainError("INVALID_IMAGE", "图片编码无效", 422) from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise DomainError("INVALID_IMAGE", "图片不能为空且不得超过1 MiB", 422)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                actual = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format or "")
                width, height = image.size
                if (
                    actual != media_type
                    or actual is None
                    or width * height > MAX_IMAGE_PIXELS
                    or max(width, height) > 4096
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("Unsupported format, dimensions or animation")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise DomainError(
            "INVALID_IMAGE", "仅支持完整、静态PNG/JPEG，最长边4096且最多400万像素", 422
        ) from exc
    return ImportedImage(data, hashlib.sha256(data).hexdigest(), media_type, width, height)


def next_status(current: str, target: str, archived_from: str | None) -> str:
    if current == "ARCHIVED" and target == "RESTORE" and archived_from in TRANSITIONS:
        return archived_from
    if target not in TRANSITIONS.get(current, set()):
        raise DomainError(
            "ASSET_VERSION_STATE_CONFLICT", "版本状态已改变或不允许此转换，请刷新", 409
        )
    return target
