"""Decode in a credential-free child with CPU, memory and wall-clock limits."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore

from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES, check_file_policy

PARSER_TIMEOUT = 20
_slots = BoundedSemaphore(2)


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


def probe(data: bytes, media_type: str) -> dict:
    policy = POLICIES.get(media_type)
    if policy is None or not 0 < len(data) <= policy.max_bytes:
        raise DomainError("INVALID_FILE", "文件类型或大小不符合允许范围", 422)
    if not _slots.acquire(timeout=3):
        raise DomainError("VALIDATION_BUSY", "文件校验繁忙，请重试", 503)
    try:
        # No database/S3/provider credentials, project paths or user filename reach the child.
        environment = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
        environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        with (
            tempfile.TemporaryDirectory(prefix="arclight-probe-") as folder,
            (Path(folder) / "result.json").open("w+b") as output,
        ):
            result = subprocess.run(
                [sys.executable, "-I", "-m", "arclight.media.probe", media_type],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                cwd=folder,
                env=environment,
                timeout=PARSER_TIMEOUT,
                creationflags=creation_flags,
            )
            output.seek(0)
            raw = output.read(4097)
        if result.returncode or len(raw) > 4096:
            raise ValueError("Parser refused file")
        details = json.loads(raw)
        if not isinstance(details, dict) or details.get("media_type") != media_type:
            raise ValueError("Invalid parser result")
        return details
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise DomainError("INVALID_FILE", "文件无效、不完整或超出安全解析限制", 422) from None
    finally:
        _slots.release()
