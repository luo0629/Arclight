from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.model_services import cipher, validate_endpoint
from arclight.core.errors import DomainError
from arclight.persistence.models import ModelService

Kind = Literal["text", "image", "video", "speech"]
router = APIRouter(prefix="/api/v1/me/model-services", tags=["model-services"])


class ServiceInput(Input):
    mode: Literal["server", "custom"] = "server"
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=160)
    api_key: SecretStr | None = Field(default=None, max_length=2000)
    clear_key: bool = False
    expected_revision: int = Field(ge=0)


def user_id(actor: str) -> UUID:
    if actor == "service-owner":
        raise DomainError("ACCOUNT_REQUIRED", "服务配置需要使用个人账号登录", 403)
    return UUID(actor)


def public(row: ModelService) -> dict:
    return {
        "kind": row.kind,
        "mode": row.mode,
        "base_url": row.base_url,
        "model": row.model,
        "has_key": bool(row.encrypted_key),
        "revision": row.revision,
    }


@router.get("")
def list_services(db: Database, actor: Actor, request: Request, response: Response) -> dict:
    rows = db.scalars(select(ModelService).where(ModelService.user_id == user_id(actor)))
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    return {
        "services": [public(row) for row in rows],
        "can_store_keys": bool(settings.service_encryption_key.get_secret_value()),
        "text_runtime": "mock" if settings.provider == "mock" else "服务器文字服务",
        "allowed_hosts": settings.model_service_allowed_hosts,
    }


@router.put("/{kind}")
def save_service(
    kind: Kind,
    payload: ServiceInput,
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> dict:
    uid = user_id(actor)
    row = db.scalar(
        select(ModelService).where(ModelService.user_id == uid, ModelService.kind == kind)
    )
    if payload.expected_revision != (row.revision if row else 0):
        raise DomainError("SERVICE_CONFLICT", "配置已在其他页面更新，请重新打开后再保存", 409)
    settings = request.app.state.settings
    if kind != "text" and payload.mode == "custom":
        raise DomainError("SERVICE_NOT_READY", "此服务生成链路尚未开放，只能保存预设", 422)
    base = payload.base_url
    if base:
        try:
            base = validate_endpoint(base, settings, active=payload.mode == "custom")
        except ValueError:
            raise DomainError("INVALID_SERVICE_URL", "服务地址格式不正确", 422) from None
    encrypted = row.encrypted_key if row else None
    if payload.clear_key:
        encrypted = None
    if payload.api_key and payload.api_key.get_secret_value():
        encrypted = cipher(settings).encrypt(payload.api_key.get_secret_value().encode()).decode()
    if payload.mode == "custom" and (not base or not payload.model or not encrypted):
        raise DomainError("SERVICE_INCOMPLETE", "启用文字服务需要地址、模型和已保存密钥", 422)
    values = dict(
        mode=payload.mode,
        base_url=base,
        model=payload.model,
        encrypted_key=encrypted,
        revision=payload.expected_revision + 1,
    )
    if row:
        result = db.execute(
            update(ModelService)
            .where(ModelService.id == row.id, ModelService.revision == payload.expected_revision)
            .values(**values)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise DomainError("SERVICE_CONFLICT", "配置已更新，请重新打开后再保存", 409)
        db.refresh(row)
    else:
        row = ModelService(user_id=uid, kind=kind, **values)
        db.add(row)
        db.flush()
    response.headers["Cache-Control"] = "no-store"
    return public(row)
