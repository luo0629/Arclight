from datetime import UTC, datetime, timedelta
from functools import partial
from threading import BoundedSemaphore
from time import monotonic
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy import or_, select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.model_services import (
    agent_capabilities,
    run_settings,
    text_credential_identity,
    text_response_format,
    validate_endpoint,
)
from arclight.application.service_credentials import (
    check_writer,
    key_state,
    read_credential,
    storage_status,
    store_credential,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.core.service_keys import keyring
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.model_suppliers import (
    PROTOCOLS,
    SPEECH_MODEL_PRESETS,
    SUPPLIERS,
    Supplier,
    supplier_by_id,
)
from arclight.domain.text_model_catalog import TEXT_MODELS
from arclight.persistence.models import ModelService, ServiceAudit
from arclight.providers.media_io import bounded_call
from arclight.providers.model_capability_probe import probe_model_capabilities
from arclight.providers.service_connection import ConnectionCheck, probe_connection

Kind = Literal["text", "image", "video", "speech"]
router = APIRouter(prefix="/api/v1/me/model-services", tags=["model-services"])
_CAPABILITY_PROBE_SLOTS = BoundedSemaphore(2)
_CAPABILITY_PROBE_LEASE = timedelta(minutes=2)


class ServiceConnectionInput(Input):
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=160)
    supplier: str = Field(default="custom", max_length=40)
    protocol: str = Field(default="", max_length=40)
    api_key: SecretStr | None = Field(default=None, max_length=2000)
    expected_revision: int = Field(ge=0)


class ServiceInput(ServiceConnectionInput):
    supplier_name: str = Field(default="", max_length=100)
    clear_key: bool = False


def user_id(actor: str) -> UUID:
    if actor == "service-owner":
        raise DomainError("ACCOUNT_REQUIRED", "服务配置需要使用个人账号登录", 403)
    return UUID(actor)


class CapabilityCheckInput(Input):
    expected_revision: int = Field(ge=1)
    confirm_charges: Literal[True]


def capability_configuration(row: ModelService) -> dict:
    return {
        "base_url": row.base_url,
        "model": row.model,
        "supplier": row.supplier,
        "protocol": row.protocol,
        "verified_capabilities": row.verified_capabilities,
    }


def public(row: ModelService, settings: Settings, key_status: str) -> dict:
    result = {
        "kind": row.kind,
        "base_url": row.base_url,
        "model": row.model,
        "supplier": row.supplier,
        "supplier_name": row.supplier_name,
        "protocol": row.protocol,
        "has_key": row.credential_id is not None,
        "key_status": key_status,
        "revision": row.revision,
    }
    if row.kind == "text":
        capabilities = agent_capabilities(settings, capability_configuration(row))
        tools = capabilities.tools if capabilities else False
        images = capabilities.max_input_images if capabilities else 0
        format_supported = bool(capabilities and capabilities.response_formats)
        result["capability_check"] = {
            "status": "ready"
            if format_supported and tools and images == 8
            else "partial"
            if format_supported
            else "unverified",
            "tools": tools,
            "max_input_images": images,
            "can_check": capabilities is None and key_status == "READY",
        }
    return result


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
        key_status = "MISSING"
        if row.credential_id is not None:
            try:
                read_credential(db, settings, {"credential_id": str(row.credential_id)})
                key_status = "READY"
            except DomainError as exc:
                key_status = exc.code
        profiles.append(public(row, settings, key_status))
    return {
        "services": profiles,
        "can_store_keys": status["code"] == "READY",
        "key_storage": status,
        "protocols": PROTOCOLS,
        "text_models": [
            {"supplier": supplier, "model": model, "name": name}
            for supplier, model, name, _, _ in TEXT_MODELS
        ],
        "speech_models": [preset.model_dump() for preset in SPEECH_MODEL_PRESETS],
        "suppliers": [preset.model_dump(exclude={"response_format"}) for preset in SUPPLIERS],
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
        if row is None or row.credential_id is None:
            raise DomainError("SERVICE_INCOMPLETE", "请填写当前服务的 API Key", 422)
        if (row.base_url, row.supplier, row.protocol) != (base, payload.supplier, protocol):
            raise DomainError(
                "SERVICE_KEY_REENTRY_REQUIRED", "服务地址或协议已改变，请重新填写 API Key", 422
            )
        key = read_credential(db, settings, {"credential_id": str(row.credential_id)})

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
    name = preset.name if preset else payload.supplier_name.strip()
    model = payload.model.strip()
    if not model:
        raise DomainError("SERVICE_MODEL_REQUIRED", "请填写服务商文档中的模型 ID", 422)
    has_new_key = bool(payload.api_key and payload.api_key.get_secret_value())
    if payload.clear_key and has_new_key:
        raise DomainError("INVALID_KEY_CHANGE", "清除和替换密钥不能同时进行", 422)
    destination_changed = bool(
        row and (row.base_url, row.supplier, row.protocol) != (base, payload.supplier, protocol)
    )
    if (
        row
        and row.credential_id is not None
        and destination_changed
        and not (has_new_key or payload.clear_key)
    ):
        raise DomainError(
            "SERVICE_KEY_REENTRY_REQUIRED",
            "供应商或服务地址已改变，请重新填写 API Key 或清除原密钥",
            422,
        )
    credential_id = row.credential_id if row else None
    if credential_id and not payload.clear_key:
        check_writer(state, keyring(settings))
    if payload.clear_key:
        credential_id = None
    if has_new_key:
        assert payload.api_key is not None
        credential_id = store_credential(db, settings, payload.api_key.get_secret_value()).id
    if not base or (credential_id is None and not (payload.clear_key and row)):
        raise DomainError(
            "SERVICE_INCOMPLETE", "请填写 API Key 和模型 ID；自定义服务还需 Base URL", 422
        )
    if credential_id is not None:
        read_credential(db, settings, {"credential_id": str(credential_id)})
    same_route = row is not None and not destination_changed and row.model == model
    verified_capabilities = (
        row.verified_capabilities
        if row and same_route and not has_new_key and not payload.clear_key
        else None
    )
    values = dict(
        base_url=base,
        model=model,
        supplier=payload.supplier,
        supplier_name=name,
        verified_capabilities=verified_capabilities,
        protocol=protocol,
        credential_id=credential_id,
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
    return public(row, settings, "READY" if credential_id is not None else "MISSING")


_PROBE_ERRORS = {
    "PROVIDER_AUTHENTICATION_FAILED": ("服务拒绝了 API Key，请检查凭证。", 422),
    "PROVIDER_ACCESS_DENIED": ("当前账号没有使用此模型的权限。", 422),
    "PROVIDER_INSUFFICIENT_BALANCE": ("账户余额不足", 402),
    "PROVIDER_QUOTA_EXHAUSTED": ("供应商报告账号配额已用完，请检查供应商账号。", 422),
    "PROVIDER_RATE_LIMITED": ("供应商限制了请求频率，请稍后再试。", 429),
    "SERVICE_ADDRESS_BLOCKED": ("此模型服务地址无法通过公网访问校验。", 422),
    "SERVICE_DNS_UNAVAILABLE": ("无法解析模型服务地址。", 502),
    "SERVICE_REDIRECT_BLOCKED": ("模型服务返回重定向，请检查服务地址。", 422),
}


def _verify_text_service(
    session_factory: sessionmaker[Session],
    service_id: UUID,
    revision: int,
    actor: str,
    settings: Settings,
    probe_settings: Settings,
    credential_identity: tuple[str, ...],
    deadline: float,
) -> dict:
    claim = uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as writer:
        result = writer.execute(
            update(ModelService)
            .where(
                ModelService.id == service_id,
                ModelService.revision == revision,
                or_(
                    ModelService.capability_probe_until.is_(None),
                    ModelService.capability_probe_until <= now,
                ),
            )
            .values(capability_probe_id=claim, capability_probe_until=now + _CAPABILITY_PROBE_LEASE)
        )
        if cast(CursorResult, result).rowcount != 1:
            current_service(writer, user_id(actor), "text", revision)
            raise DomainError("SERVICE_CAPABILITY_CHECK_RUNNING", "当前模型正在验证，请稍候。", 409)

    claimed_row = (ModelService.id == service_id, ModelService.capability_probe_id == claim)
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        bounded_settings = probe_settings.model_copy(update={"llm_timeout_seconds": remaining})
        capabilities = probe_model_capabilities(bounded_settings, credential_identity)
        if capabilities is None:
            raise DomainError(
                "SERVICE_CAPABILITY_UNSUPPORTED",
                "此模型未通过结构化输出验证，请核对协议或更换模型。",
                422,
            )
        with session_factory.begin() as writer:
            result = writer.execute(
                update(ModelService)
                .where(*claimed_row, ModelService.revision == revision)
                .values(
                    verified_capabilities=capabilities.model_dump(mode="json"),
                    revision=revision + 1,
                    capability_probe_id=None,
                    capability_probe_until=None,
                )
            )
            if cast(CursorResult, result).rowcount != 1:
                raise DomainError("SERVICE_CONFLICT", "配置已更新，请重新打开后再验证", 409)
            row = writer.get(ModelService, service_id)
            assert row is not None
            writer.add(
                ServiceAudit(
                    user_id=row.user_id,
                    actor=actor,
                    action="service.capabilities_verified",
                    details_json={"kind": "text", "config_revision": row.revision},
                )
            )
            return public(row, settings, "READY")
    except Exception:
        with session_factory.begin() as writer:
            writer.execute(
                update(ModelService)
                .where(*claimed_row)
                .values(capability_probe_id=None, capability_probe_until=None)
            )
        raise


@router.post("/text/capabilities")
def check_text_capabilities(
    payload: CapabilityCheckInput,
    db: Database,
    actor: Actor,
    request: Request,
    response: Response,
) -> dict:
    uid = user_id(actor)
    settings = request.app.state.settings
    row = current_service(db, uid, "text", payload.expected_revision)
    if row is None or row.credential_id is None:
        raise DomainError("SERVICE_INCOMPLETE", "请先在网页保存文字模型和 API Key", 422)
    response.headers["Cache-Control"] = "no-store"
    route = {
        **capability_configuration(row),
        "credential_id": str(row.credential_id),
    }
    route["response_format"] = text_response_format(route)
    service_id = row.id
    probe_settings = run_settings(settings, route, db)
    if agent_capabilities(settings, route) is not None:
        return public(row, settings, "READY")
    credential_identity = text_credential_identity(actor, route)
    timeout = min(settings.llm_timeout_seconds, 45)
    db.rollback()
    try:
        # The provider thread owns the claim even if the HTTP request stops waiting.
        return bounded_call(
            partial(
                _verify_text_service,
                request.app.state.session_factory,
                service_id,
                payload.expected_revision,
                actor,
                settings,
                probe_settings,
                credential_identity,
                monotonic() + timeout,
            ),
            timeout + 2,
            slots=_CAPABILITY_PROBE_SLOTS,
        )
    except TimeoutError:
        raise DomainError(
            "SERVICE_CAPABILITY_CHECK_TIMEOUT", "能力检查超时或繁忙，请先核对供应商调用记录。", 503
        ) from None
    except GatewayFailure as exc:
        message, status = _PROBE_ERRORS.get(
            exc.code, ("未能确认验证结果，请检查供应商调用记录和模型设置。", 502)
        )
        code = (
            "ACCOUNT_BALANCE_INSUFFICIENT" if status == 402 else "SERVICE_CAPABILITY_CHECK_FAILED"
        )
        raise DomainError(code, message, status) from None
