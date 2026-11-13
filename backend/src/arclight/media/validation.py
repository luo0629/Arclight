"""Decode in a credential-free child with CPU, memory and wall-clock limits."""

import hashlib
import json
from dataclasses import dataclass

from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES, check_file_policy
from arclight.media.execution import MediaExecutionError, MediaObserver, execute_media

PARSER_TIMEOUT = 20


@dataclass(frozen=True)
class ValidatedFile:
    data: bytes
    checksum: str
    media_type: str
    details: dict


def validate_file(
    data: bytes, purpose: str, name: str, media_type: str, checksum: str | None = None
) -> ValidatedFile:
    check_file_policy(purpose, name, media_type, len(data))
    digest = hashlib.sha256(data).hexdigest()
    if checksum is not None and digest != checksum:
        raise DomainError("INVALID_FILE", "文件哈希与声明不一致", 422)
    details = probe(data, media_type)
    return ValidatedFile(data, digest, media_type, details)


def probe(data: bytes, media_type: str, *, observer: MediaObserver | None = None) -> dict:
    policy = POLICIES.get(media_type)
    if (
        policy is None
        or not data
        or (policy.max_bytes is not None and len(data) > policy.max_bytes)
    ):
        raise DomainError("INVALID_FILE", "文件类型或大小不符合允许范围", 422)
    try:
        raw = execute_media("probe", media_type, data, timeout=PARSER_TIMEOUT, observer=observer)
        details = json.loads(raw)
        if not isinstance(details, dict) or details.get("media_type") != media_type:
            raise ValueError("Invalid parser result")
        return details
    except MediaExecutionError as exc:
        if exc.reason == "BUSY":
            raise DomainError("VALIDATION_BUSY", "文件校验繁忙，请重试", 503) from None
        raise DomainError("INVALID_FILE", "文件无效、不完整或超出安全解析限制", 422) from None
    except ValueError:
        raise DomainError("INVALID_FILE", "文件无效、不完整或超出安全解析限制", 422) from None
