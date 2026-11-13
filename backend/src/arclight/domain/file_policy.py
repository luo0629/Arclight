"""The accepted upload contract, shared by capability issuance and validation."""

from dataclasses import dataclass
from pathlib import PurePath

from arclight.core.errors import DomainError


@dataclass(frozen=True)
class FilePolicy:
    purpose: frozenset[str]
    extensions: frozenset[str]
    max_bytes: int | None


POLICIES = {
    "video/mp4": FilePolicy(frozenset({"GENERATED_VIDEO"}), frozenset({".mp4"}), None),
    "audio/flac": FilePolicy(frozenset({"GENERATED_AUDIO"}), frozenset({".flac"}), None),
    "image/png": FilePolicy(frozenset({"REFERENCE_IMAGE"}), frozenset({".png"}), None),
    "image/jpeg": FilePolicy(frozenset({"REFERENCE_IMAGE"}), frozenset({".jpg", ".jpeg"}), None),
    "text/plain": FilePolicy(frozenset({"STORY"}), frozenset({".txt"}), 256 * 1024),
    "text/markdown": FilePolicy(frozenset({"STORY"}), frozenset({".md", ".markdown"}), 256 * 1024),
    "audio/wav": FilePolicy(frozenset({"AUDIO", "MUSIC"}), frozenset({".wav"}), None),
    "audio/mpeg": FilePolicy(frozenset({"AUDIO", "MUSIC"}), frozenset({".mp3"}), None),
    "audio/ogg": FilePolicy(frozenset({"AUDIO", "MUSIC"}), frozenset({".ogg", ".opus"}), None),
    "font/ttf": FilePolicy(frozenset({"FONT"}), frozenset({".ttf"}), 8 * 1024**2),
    "font/otf": FilePolicy(frozenset({"FONT"}), frozenset({".otf"}), 8 * 1024**2),
}


def check_file_policy(purpose: str, name: str, media_type: str, byte_size: int) -> FilePolicy:
    policy = POLICIES.get(media_type)
    if (
        policy is None
        or purpose not in policy.purpose
        or byte_size <= 0
        or (policy.max_bytes is not None and byte_size > policy.max_bytes)
        or not name
        or len(name) > 160
        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\:" for c in name)
        or name != name.strip()
        or PurePath(name).suffix.lower() not in policy.extensions
    ):
        raise DomainError("INVALID_FILE", "文件用途、扩展名、类型或大小不符合允许范围", 422)
    return policy
