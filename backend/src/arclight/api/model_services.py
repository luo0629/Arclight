from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy import select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.model_services import validate_endpoint
from arclight.application.service_credentials import (
    check_writer,
    credential_snapshot,
    key_state,
    read_credential,
    storage_status,
    store_credential,
)
from arclight.core.errors import DomainError
from arclight.core.service_keys import keyring
from arclight.domain.model_suppliers import (
    PROTOCOLS,
    SPEECH_MODEL_PRESETS,
    SUPPLIERS,
    Supplier,
    supplier_by_id,
)
from arclight.persistence.models import ModelService, ServiceAudit
from arclight.providers.service_connection import ConnectionCheck, probe_connection

Kind = Literal["text", "image", "video", "speech"]
router = APIRouter(prefix="/api/v1/me/model-services", tags=["model-services"])


class ServiceConnectionInput(Input):
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=160)
    supplier: str = Field(default="custom", max_length=40)
    protocol: str = Field(default="", max_length=40)
    api_key: SecretStr | None = Field(default=None, max_length=2000)
    expected_revision: int = Field(ge=0)


class ServiceInput(ServiceConnectionInput):
    supplier_name: str = Field(default="", max_length=100)
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    clear_key: bool = False


def user_id(actor: str) -> UUID:
    if actor == "service-owner":
        raise DomainError("ACCOUNT_REQUIRED", "服务配置需要使用个人账号登录", 403)
    return UUID(actor)


def public(row: ModelService) -> dict:
    return {
        "kind": row.kind,
        "base_url": row.base_url,
        "model": row.model,
        "supplier": row.supplier,
        "supplier_name": row.supplier_name,
        "response_format": row.response_format,
        "protocol": row.protocol,
        "has_key": bool(row.encrypted_key or row.credential_id),
        "revision": row.revision,
    }


@router.get("")
def list_services(
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> dict:
    rows = list(db.scalars(select(ModelService).where(ModelService.user_id == user_id(actor))))
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    status = storage_status(db, settings)
    profiles = []
    for row in rows:
        value = public(row)
        value["key_status"] = "MISSING"
        if value["has_key"]:
            try:
                read_credential(db, settings, credential_snapshot(row))
                value["key_status"] = "READY"
            except DomainError as exc:
                value["key_status"] = exc.code
        profiles.append(value)
    return {
        "services": profiles,
        "can_store_keys": status["code"] == "READY",
        "key_storage": status,
        "catalog_version": 2,
        "protocols": PROTOCOLS,
        "speech_models": [preset.model_dump() for preset in SPEECH_MODEL_PRESETS],
        "suppliers": [preset.model_dump() for preset in SUPPLIERS],
    }


@router.get("/audit")
def list_audit(
    db: Database,
    actor: Actor,
    response: Response,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict:
    uid = user_id(actor)
    query = select(ServiceAudit).where(ServiceAudit.user_id == uid)
    if before:
        cursor = db.get(ServiceAudit, before)
        if cursor is None or cursor.user_id != uid:
            raise DomainError("NOT_FOUND", "审计记录不存在", 404)
        query = query.where(
            tuple_(ServiceAudit.created_at, ServiceAudit.id) < (cursor.created_at, cursor.id)
        )
    rows = list(
        db.scalars(
            query.order_by(ServiceAudit.created_at.desc(), ServiceAudit.id.desc()).limit(limit + 1)
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {
                "id": str(row.id),
                "action": row.action,
                "details": row.details_json,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


def current_service(
    db: Session, uid: UUID, kind: Kind, expected_revision: int
) -> ModelService | None:
    row = db.scalar(
        select(ModelService).where(ModelService.user_id == uid, ModelService.kind == kind)
    )
    if expected_revision != (row.revision if row else 0):
        raise DomainError("SERVICE_CONFLICT", "配置已在其他页面更新，请重新打开后再操作", 409)
    return row


def service_destination(
    kind: Kind, payload: ServiceConnectionInput
) -> tuple[Supplier | None, str, str]:
    preset = supplier_by_id(payload.supplier, kind)
    if payload.supplier != "custom" and preset is None:
        raise DomainError("INVALID_SUPPLIER", "请选择当前类型的服务商或自定义", 422)
    protocol = preset.protocol if preset else (payload.protocol or PROTOCOLS[kind][0])
    if protocol not in PROTOCOLS[kind] or (
        preset and payload.protocol and protocol != payload.protocol
    ):
        raise DomainError("INVALID_PROTOCOL", "协议与服务类型或预设不匹配", 422)
    base = payload.base_url.strip() or (preset.base_url if preset else "")
    if base:
        base = validate_endpoint(base)
    if preset and base != preset.base_url:
        raise DomainError("SUPPLIER_MISMATCH", "预设地址已变化，请切换自定义供应商", 422)
    return preset, base, protocol


@router.post("/{kind}/connection")
def check_connection(
    kind: Kind,
    payload: ServiceConnectionInput,
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> ConnectionCheck:
    row = current_service(db, user_id(actor), kind, payload.expected_revision)
    _, base, protocol = service_destination(kind, payload)
    model = payload.model.strip()
    if not base or not model:
        raise DomainError("SERVICE_INCOMPLETE", "请填写模型 ID，自定义服务还需 Base URL", 422)
    settings = request.app.state.settings
    key = payload.api_key.get_secret_value() if payload.api_key else ""
    if not key:
        if row is None or not (row.credential_id or row.encrypted_key):
            raise DomainError("SERVICE_INCOMPLETE", "请填写当前服务的 API Key", 422)
        stored_protocol = row.protocol or ("openai_chat" if kind == "text" else "")
        if (row.base_url, row.supplier, stored_protocol) != (base, payload.supplier, protocol):
            raise DomainError(
                "SERVICE_KEY_REENTRY_REQUIRED", "服务地址或协议已改变，请重新填写 API Key", 422
            )
        key = read_credential(db, settings, credential_snapshot(row))

    # This endpoint only reads account configuration. Release the DB transaction before I/O.
    db.rollback()
    response.headers["Cache-Control"] = "no-store"
    return probe_connection(base, protocol, model, key, settings.llm_dns_timeout_seconds)


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
    # Same lock order as rotation, including when preserving an existing credential.
    state = key_state(db)
    row = current_service(db, uid, kind, payload.expected_revision)
    settings = request.app.state.settings
    preset, base, protocol = service_destination(kind, payload)
    response_format = (
        preset.response_format
        if preset and "response_format" not in payload.model_fields_set
        else payload.response_format
    )
    if preset and response_format != preset.response_format:
        raise DomainError("SUPPLIER_MISMATCH", "预设地址或输出格式已变化，请切换自定义供应商", 422)
    name = preset.name if preset else payload.supplier_name.strip()
    model = payload.model.strip()
    if not model:
        raise DomainError("SERVICE_MODEL_REQUIRED", "请填写服务商文档中的模型 ID", 422)
    has_new_key = bool(payload.api_key and payload.api_key.get_secret_value())
    if payload.clear_key and has_new_key:
        raise DomainError("INVALID_KEY_CHANGE", "清除和替换密钥不能同时进行", 422)
    destination_changed = bool(
        row
        and (
            row.base_url != base
            or row.supplier != payload.supplier
            or (row.protocol and row.protocol != protocol)
        )
    )
    if (
        row
        and (row.credential_id or row.encrypted_key)
        and destination_changed
        and not (has_new_key or payload.clear_key)
    ):
        raise DomainError(
            "SERVICE_KEY_REENTRY_REQUIRED",
            "供应商或服务地址已改变，请重新填写 API Key 或清除原密钥",
            422,
        )
    encrypted = row.encrypted_key if row else None
    credential_id = row.credential_id if row else None
    if (encrypted or credential_id) and not payload.clear_key:
        check_writer(state, keyring(settings))
    if payload.clear_key:
        encrypted = None
        credential_id = None
    if has_new_key:
        assert payload.api_key is not None
        credential_id = store_credential(db, settings, payload.api_key.get_secret_value()).id
        encrypted = None
    if not base or (not (encrypted or credential_id) and not (payload.clear_key and row)):
        raise DomainError(
            "SERVICE_INCOMPLETE", "请填写 API Key 和模型 ID；自定义服务还需 Base URL", 422
        )
    if encrypted or credential_id:
        read_credential(
            db,
            settings,
            {"credential_id": str(credential_id)}
            if credential_id
            else {"encrypted_key": encrypted},
        )
    values = dict(
        base_url=base,
        model=model,
        supplier=payload.supplier,
        supplier_name=name,
        response_format=response_format,
        protocol=protocol,
        credential_id=credential_id,
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
    db.add(
        ServiceAudit(
            user_id=uid,
            actor=actor,
            action="service.saved",
            details_json={
                "kind": kind,
                "supplier": row.supplier,
                "config_revision": row.revision,
                "key_action": "replaced"
                if has_new_key
                else "cleared"
                if payload.clear_key
                else "preserved",
                "destination_changed": destination_changed,
            },
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return public(row)
