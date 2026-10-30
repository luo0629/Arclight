"""The accepted upload contract, shared by capability issuance and validation."""

from dataclasses import dataclass
from pathlib import PurePath

from arclight.core.errors import DomainError


@dataclass(frozen=True)
class FilePolicy:
    purpose: frozenset[str]
    extensions: frozenset[str]
    max_bytes: int


POLICIES = {
    "image/png": FilePolicy(frozenset({"REFERENCE_IMAGE"}), frozenset({".png"}), 1024**2),
    "image/jpeg": FilePolicy(frozenset({"REFERENCE_IMAGE"}), frozenset({".jpg", ".jpeg"}), 1024**2),
    "text/plain": FilePolicy(frozenset({"STORY"}), frozenset({".txt"}), 256 * 1024),
    "text/markdown": FilePolicy(frozenset({"STORY"}), frozenset({".md", ".markdown"}), 256 * 1024),
    "audio/wav": FilePolicy(frozenset({"AUDIO", "MUSIC"}), frozenset({".wav"}), 32 * 1024**2),
    "audio/mpeg": FilePolicy(frozenset({"AUDIO", "MUSIC"}), frozenset({".mp3"}), 32 * 1024**2),
    "audio/ogg": FilePolicy(
        frozenset({"AUDIO", "MUSIC"}), frozenset({".ogg", ".opus"}), 32 * 1024**2
    ),
    "font/ttf": FilePolicy(frozenset({"FONT"}), frozenset({".ttf"}), 8 * 1024**2),
    "font/otf": FilePolicy(frozenset({"FONT"}), frozenset({".otf"}), 8 * 1024**2),
}


def check_file_policy(purpose: str, name: str, media_type: str, byte_size: int) -> FilePolicy:
    policy = POLICIES.get(media_type)
    if (
        policy is None
        or purpose not in policy.purpose
        or not 0 < byte_size <= policy.max_bytes
        or not name
        or len(name) > 160
        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\:" for c in name)
        or name != name.strip()
        or PurePath(name).suffix.lower() not in policy.extensions
    ):
        raise DomainError("INVALID_FILE", "文件用途、扩展名、类型或大小不符合允许范围", 422)
    return policy
