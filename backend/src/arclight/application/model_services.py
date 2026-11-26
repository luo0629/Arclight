from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.service_credentials import credential_snapshot, read_credential
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.service_models import matching_policy
from arclight.domain.text_model_catalog import TEXT_MODEL_POLICIES
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
        "verified_capabilities": row.verified_capabilities,
        **credential_snapshot(row),
    }


def require_text_snapshot(db: Session, actor: str, settings: Settings) -> dict | None:
    snapshot = text_snapshot(db, actor)
    if snapshot is not None:
        read_credential(db, settings, snapshot)
        capabilities = agent_capabilities(settings, snapshot)
        snapshot.pop("verified_capabilities", None)
        if capabilities is not None:
            snapshot["agent_capabilities"] = capabilities.model_dump(mode="json")
            snapshot["response_format"] = preferred_response_format(
                capabilities, snapshot["response_format"]
            )
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


def agent_capabilities(settings: Settings, service: dict | None) -> AgentModelCapabilities | None:
    if service is None:
        return settings.llm_agent_capabilities if settings.provider == "mock" else None
    configuration = {"kind": "text", **service}
    for policies in (settings.service_model_policies, TEXT_MODEL_POLICIES):
        policy = matching_policy(policies, configuration)
        if policy and policy.agent_capabilities is not None:
            return policy.agent_capabilities
    if evidence := service.get("verified_capabilities"):
        return AgentModelCapabilities.model_validate(evidence)
    return None


def preferred_response_format(capabilities: AgentModelCapabilities | None, default: str) -> str:
    if (
        capabilities is None
        or not capabilities.response_formats
        or default in capabilities.response_formats
    ):
        return default
    return "json_schema" if "json_schema" in capabilities.response_formats else "json_object"
