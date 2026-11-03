"""Load a write key and decryption-only predecessors without exposing secret inputs."""

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from arclight.core.errors import DomainError

if TYPE_CHECKING:
    from arclight.core.config import Settings


@dataclass(frozen=True, repr=False)
class Keyring:
    active: Fernet
    readers: MultiFernet
    key_id: str

    def decrypt(self, token: str) -> str:
        try:
            return self.readers.decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeError, ValueError, AttributeError):
            raise DomainError(
                "SERVICE_KEY_UNAVAILABLE", "服务凭证无法解密，请管理员检查主密钥或恢复备份", 409
            ) from None


def keyring(settings: "Settings") -> Keyring:
    active = settings.service_encryption_key.get_secret_value()
    previous = [key.get_secret_value() for key in settings.service_encryption_previous_keys]
    try:
        if settings.service_encryption_key_file:
            if active:
                raise ValueError
            active = settings.service_encryption_key_file.read_text(encoding="utf-8").strip()
        if settings.service_encryption_previous_keys_file:
            if previous:
                raise ValueError
            previous = json.loads(
                settings.service_encryption_previous_keys_file.read_text(encoding="utf-8")
            )
        if not isinstance(previous, list) or not all(isinstance(key, str) for key in previous):
            raise ValueError
        if not active:
            raise DomainError("KEY_STORAGE_DISABLED", "服务器尚未配置服务密钥加密", 409)
        keys = [active, *previous]
        if len(set(keys)) != len(keys):
            raise ValueError
        ciphers = [Fernet(key.encode()) for key in keys]
    except (OSError, UnicodeError, ValueError, TypeError):
        raise DomainError(
            "KEY_STORAGE_INVALID", "服务密钥加密配置无效，请管理员检查注入文件及主密钥格式", 409
        ) from None
    return Keyring(
        ciphers[0], MultiFernet(ciphers), hashlib.sha256(active.encode()).hexdigest()[:16]
    )
