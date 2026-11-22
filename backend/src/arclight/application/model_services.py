from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.service_credentials import credential_snapshot, read_credential
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.service_models import matching_policy
from arclight.persistence.models import ModelService, WorkflowRun
from arclight.providers.network import validate_endpoint

__all__ = ["require_text_snapshot", "run_settings", "text_snapshot", "validate_endpoint"]


def text_snapshot(db: Session, actor: str) -> dict | None:
    if actor == "service-owner":
        return None
    row = db.scalar(
        select(ModelService).where(ModelService.user_id == UUID(actor), ModelService.kind == "text")
    )
    if row is None or not (row.base_url and row.model and (row.credential_id or row.encrypted_key)):
        return None
    return {
        "base_url": row.base_url,
        "model": row.model,
        "supplier": row.supplier,
        "supplier_name": row.supplier_name,
        "response_format": row.response_format,
        "protocol": row.protocol or "openai_chat",
        **credential_snapshot(row),
    }


def require_text_snapshot(db: Session, actor: str, settings: Settings) -> dict | None:
    snapshot = text_snapshot(db, actor)
    if snapshot is not None:
        read_credential(db, settings, snapshot)
    elif settings.provider != "mock":
        raise DomainError("SERVICE_INCOMPLETE", "请先在模型配置中保存账号文字服务", 422)
    return snapshot


def run_settings(settings: Settings, snapshot: dict | None, db: Session | None = None) -> Settings:
    if not snapshot:
        if settings.provider == "account":
            raise DomainError("SERVICE_INCOMPLETE", "工作流缺少冻结的账号文字服务", 409)
        return settings
    base = validate_endpoint(snapshot["base_url"])
    secret = read_credential(db, settings, snapshot)
    return settings.model_copy(
        update={
            "provider": "openai_compatible",
            "llm_base_url": base,
            "llm_model": snapshot["model"],
            "llm_api_key": SecretStr(secret),
            "llm_supplier": snapshot.get("supplier", "custom"),
            "llm_response_format": snapshot.get("response_format", "json_schema"),
            "llm_protocol": snapshot.get("protocol", "openai_chat"),
        }
    )


def text_credential_identity(actor: str, snapshot: dict | None) -> tuple[str, ...]:
    if not snapshot:
        return ("SERVER", actor)
    reference = (
        snapshot.get("credential_id") or sha256(snapshot["encrypted_key"].encode()).hexdigest()
    )
    return ("ACCOUNT_SNAPSHOT", actor, reference)


def recheck_text_credential(
    db: Session, run_id: UUID, snapshot: dict | None, settings: Settings
) -> None:
    """Revalidate the frozen reference within admission, before any model reuse."""
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    if run is None or run.input_json.get("_text_service") != snapshot:
        raise DomainError("GATEWAY_ROUTE_FORBIDDEN", "文字模型快照已改变", 409)
    if snapshot and not compare_digest(
        read_credential(db, settings, snapshot).encode(),
        settings.llm_api_key.get_secret_value().encode(),
    ):
        raise DomainError("GATEWAY_ROUTE_FORBIDDEN", "文字模型凭证已改变", 409)


def agent_capabilities(settings: Settings, service: dict | None):
    if service is None:
        return settings.llm_agent_capabilities if settings.provider == "mock" else None
    policy = matching_policy(settings.service_model_policies, {"kind": "text", **service})
    return policy.agent_capabilities if policy else None
