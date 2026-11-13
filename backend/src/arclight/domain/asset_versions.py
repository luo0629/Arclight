"""Bounded manual image imports and explicit version lifecycle transitions."""

import base64
import binascii
import hashlib
from dataclasses import dataclass

from arclight.core.errors import DomainError
from arclight.media.validation import probe

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
    if media_type not in {"image/png", "image/jpeg"}:
        raise DomainError("INVALID_IMAGE", "仅支持静态PNG/JPEG图片", 422)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DomainError("INVALID_IMAGE", "图片编码无效", 422) from exc
    if not data:
        raise DomainError("INVALID_IMAGE", "图片不能为空", 422)
    try:
        details = probe(data, media_type)
    except DomainError as exc:
        raise DomainError(
            "INVALID_IMAGE", "仅支持完整、静态PNG/JPEG，最长边4096且最多400万像素", 422
        ) from exc
    return ImportedImage(
        data, hashlib.sha256(data).hexdigest(), media_type, details["width"], details["height"]
    )


def next_status(current: str, target: str, archived_from: str | None) -> str:
    if current == "ARCHIVED" and target == "RESTORE" and archived_from in TRANSITIONS:
        return archived_from
    if target not in TRANSITIONS.get(current, set()):
        raise DomainError(
            "ASSET_VERSION_STATE_CONFLICT", "版本状态已改变或不允许此转换，请刷新", 409
        )
    return target
