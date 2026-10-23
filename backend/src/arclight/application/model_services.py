from ipaddress import ip_address
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.persistence.models import ModelService


def cipher(settings: Settings) -> Fernet:
    key = settings.service_encryption_key.get_secret_value()
    if not key:
        raise DomainError("KEY_STORAGE_DISABLED", "服务器尚未配置服务密钥加密，请联系管理员", 409)
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise DomainError("KEY_STORAGE_DISABLED", "服务器服务密钥加密配置无效", 409) from None


def validate_endpoint(url: str, settings: Settings, *, active: bool = False) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        raise DomainError(
            "INVALID_SERVICE_URL", "服务地址必须是无凭证、查询参数的公网 HTTPS 地址", 422
        )
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise DomainError("INVALID_SERVICE_URL", "服务地址不能使用 IP 地址", 422)
    if active and host not in settings.model_service_allowed_hosts:
        raise DomainError("SERVICE_HOST_BLOCKED", "请管理员先将服务域名加入允许列表", 422)
    return url.rstrip("/")


def text_snapshot(db: Session, actor: str) -> dict | None:
    if actor == "service-owner":
        return None
    row = db.scalar(
        select(ModelService).where(ModelService.user_id == UUID(actor), ModelService.kind == "text")
    )
    if not row or row.mode != "custom":
        return None
    # 任务捕获不可变配置；后续修改服务不会改变已排队或等待审核任务。
    return {"base_url": row.base_url, "model": row.model, "encrypted_key": row.encrypted_key}


def run_settings(settings: Settings, snapshot: dict | None) -> Settings:
    if not snapshot:
        return settings
    validate_endpoint(snapshot["base_url"], settings, active=True)
    try:
        secret = cipher(settings).decrypt(snapshot["encrypted_key"].encode()).decode()
    except (InvalidToken, KeyError, TypeError, AttributeError):
        raise DomainError(
            "SERVICE_KEY_UNAVAILABLE", "服务密钥不可用，请检查服务器加密配置", 409
        ) from None
    return settings.model_copy(
        update={
            "provider": "openai_compatible",
            "llm_base_url": snapshot["base_url"],
            "llm_model": snapshot["model"],
            "llm_api_key": SecretStr(secret),
        }
    )
