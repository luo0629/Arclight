"""Bounded plain-text import validation; never guess an encoding or render Markdown."""

import base64
import binascii
import hashlib
import unicodedata
from dataclasses import dataclass

from arclight.core.errors import DomainError
from arclight.media.execution import MediaExecutionError, execute_media

MAX_SOURCE_BYTES = 256 * 1024
MAX_BASE64_LENGTH = 4 * ((MAX_SOURCE_BYTES + 2) // 3)


@dataclass(frozen=True)
class DecodedSource:
    raw: bytes
    text: str
    encoding: str
    source_type: str
    sha256: str


def decode_source(filename: str, media_type: str, encoding: str, encoded: str) -> DecodedSource:
    if (
        not filename
        or len(filename) > 160
        or any(char in filename for char in "/\\:")
        or any(unicodedata.category(char).startswith("C") for char in filename)
    ):
        raise DomainError("INVALID_SOURCE_NAME", "文件名不可包含路径或控制字符", 422)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in {"txt", "md", "markdown"}:
        raise DomainError("SOURCE_TYPE_UNSUPPORTED", "仅支持 TXT、MD 或 Markdown 文件", 422)
    allowed = {"", "application/octet-stream", "text/plain"}
    if extension != "txt":
        allowed |= {"text/markdown", "text/x-markdown"}
    if media_type not in allowed:
        raise DomainError("SOURCE_TYPE_UNSUPPORTED", "文件 MIME 类型与文本格式不符", 422)
    if len(encoded) > MAX_BASE64_LENGTH:
        raise DomainError("SOURCE_TOO_LARGE", "故事文件不能超过 256 KiB", 413)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise DomainError("INVALID_SOURCE_DATA", "文件数据不是有效的 Base64", 422) from None
    if len(raw) > MAX_SOURCE_BYTES:
        raise DomainError("SOURCE_TOO_LARGE", "故事文件不能超过 256 KiB", 413)
    if not raw:
        raise DomainError("SOURCE_EMPTY", "故事文件不能为空", 422)
    if raw.startswith((b"%PDF", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"MZ")):
        raise DomainError("SOURCE_NOT_TEXT", "文件内容不是支持的纯文本", 422)
    if encoding not in {"utf-8", "gb18030"}:
        raise DomainError("SOURCE_ENCODING", "请选择 UTF-8 或 GB18030 编码", 422)
    try:
        text = execute_media("story", encoding, raw).decode("utf-8")
    except MediaExecutionError as exc:
        messages = {
            "SOURCE_ENCODING": "无法按所选编码解码，请转换为 UTF-8 或选择正确编码",
            "SOURCE_NOT_TEXT": "文件含二进制、控制字符或损坏的文本",
            "SOURCE_EMPTY": "故事文件不能只有空白",
        }
        if exc.reason in messages:
            raise DomainError(exc.reason, messages[exc.reason], 422) from None
        if exc.reason == "BUSY":
            raise DomainError("VALIDATION_BUSY", "文件校验繁忙，请重试", 503) from None
        raise DomainError("SOURCE_PARSE_LIMIT", "故事文件无效或超出安全解析限制", 422) from None
    except UnicodeError:
        raise DomainError("SOURCE_PARSE_LIMIT", "故事文件无效或超出安全解析限制", 422) from None
    actual_encoding = (
        "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") and encoding == "utf-8" else encoding
    )
    return DecodedSource(
        raw,
        text,
        actual_encoding,
        "TXT" if extension == "txt" else "MARKDOWN",
        hashlib.sha256(raw).hexdigest(),
    )
