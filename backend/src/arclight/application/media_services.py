"""One account resolver for quotes, job capture and immutable Worker credentials."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.service_credentials import read_credential
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationSpec
from arclight.domain.service_models import matching_policy
from arclight.domain.versions import digest
from arclight.persistence.models import GenerationJob, GenerationServiceBinding, ModelService
from arclight.providers.native_media import MediaConnection
from arclight.providers.native_media_protocols import KINDS, validate_spec
from arclight.providers.network import validate_endpoint

SERVICE_KINDS = {"IMAGE": "image", "VIDEO": "video", "AUDIO": "speech"}


@dataclass(frozen=True)
class MediaSelection:
    configuration: dict
    credential_id: UUID
    created_by: str
    revision: int

    def private_snapshot(self) -> dict:
        return {
            "configuration": self.configuration,
            "credential_id": str(self.credential_id),
            "created_by": self.created_by,
            "revision": self.revision,
        }


def account_selection(db: Session, actor: str, task_type: str) -> MediaSelection | None:
    if actor == "service-owner" or task_type not in SERVICE_KINDS:
        return None
    row = db.scalar(
        select(ModelService).where(
            ModelService.user_id == UUID(actor), ModelService.kind == SERVICE_KINDS[task_type]
        )
    )
    if row is None or row.mode != "custom":
        return None
    settings = db.info.get("settings")
    if settings is None:
        raise DomainError("MEDIA_SERVICE_NOT_READY", "媒体服务运行配置缺失", 409)
    if row.protocol not in KINDS or KINDS[row.protocol] != task_type:
        raise DomainError("INVALID_PROTOCOL", "请重新保存媒体服务以选择原生协议", 422)
    configuration = {
        "kind": row.kind,
        "supplier": row.supplier,
        "protocol": row.protocol,
        "provider": row.protocol,
        "model": row.model,
        "base_url": validate_endpoint(row.base_url),
        "config_revision": row.revision,
    }
    policy = matching_policy(settings.service_model_policies, configuration)
    configuration["maximum_cost_per_output"] = (
        str(policy.maximum_cost_per_output)
        if policy and policy.maximum_cost_per_output is not None
        else None
    )
    configuration["pricing_version"] = policy.pricing_version if policy else ""
    configuration["configuration_version"] = digest(configuration)
    credential_id = row.credential_id
    if credential_id is None:
        raise DomainError("SERVICE_INCOMPLETE", "请先保存媒体服务的 API Key 和模型 ID", 422)
    return MediaSelection(configuration, credential_id, actor, row.revision)


def restore_selection(snapshot: dict) -> MediaSelection:
    config = snapshot["configuration"]
    if config.get("configuration_version") != digest(
        {key: value for key, value in config.items() if key != "configuration_version"}
    ):
        raise DomainError("MEDIA_SERVICE_CONFIG_CHANGED", "冻结的媒体配置摘要不匹配", 409)
    return MediaSelection(
        config, UUID(snapshot["credential_id"]), snapshot["created_by"], snapshot["revision"]
    )


def selection_for_job(db: Session, job: GenerationJob) -> MediaSelection | None:
    if job.provider not in KINDS:
        return None
    binding = db.scalar(
        select(GenerationServiceBinding).where(GenerationServiceBinding.job_id == job.id)
    )
    if binding is None:
        if job.provider in KINDS:
            raise DomainError("MEDIA_SERVICE_BINDING_MISSING", "媒体任务缺少原始账号凭证绑定", 409)
        return None
    if binding.configuration_digest != digest(binding.configuration_json):
        raise DomainError("MEDIA_SERVICE_CONFIG_CHANGED", "原始媒体凭证绑定不一致", 409)
    return MediaSelection(
        binding.configuration_json,
        binding.credential_id,
        binding.created_by,
        binding.configuration_revision,
    )


def select_for_spec(
    db: Session, actor: str, spec: GenerationSpec, task_type: str, frozen: dict | None = None
) -> MediaSelection | None:
    if spec.provider not in KINDS:
        return None
    selection = restore_selection(frozen) if frozen else account_selection(db, actor, task_type)
    if selection is None:
        raise DomainError("SERVICE_INCOMPLETE", "请先在账号设置中配置对应媒体服务", 422)
    config = selection.configuration
    if (spec.provider, spec.model, spec.model_config_version) != (
        config["provider"],
        config["model"],
        config["configuration_version"],
    ):
        raise DomainError(
            "MEDIA_SERVICE_CONFIG_CHANGED", "媒体服务已变化，请重新读取配置并创建任务", 409
        )
    validate_spec(spec)
    return selection


def quote_selection(
    db: Session, selection: MediaSelection, spec: GenerationSpec
) -> CostQuote | None:
    settings = db.info.get("settings")
    policy = (
        matching_policy(settings.service_model_policies, selection.configuration)
        if settings
        else None
    )
    if policy is None or policy.maximum_cost_per_output is None or not policy.pricing_version:
        return None
    if (
        str(policy.maximum_cost_per_output) != selection.configuration["maximum_cost_per_output"]
        or policy.pricing_version != selection.configuration["pricing_version"]
    ):
        raise DomainError("BUDGET_QUOTE_CHANGED", "部署侧可信报价已变化，请重新创建任务", 409)
    return CostQuote(
        maximum_cost=policy.maximum_cost_per_output * spec.batch_size,
        pricing_version=policy.pricing_version,
    )


def bind_selection(db: Session, job: GenerationJob, selection: MediaSelection | None) -> None:
    if selection is None:
        return
    db.add(
        GenerationServiceBinding(
            job_id=job.id,
            created_by=selection.created_by,
            credential_id=selection.credential_id,
            configuration_revision=selection.revision,
            configuration_json=selection.configuration,
            configuration_digest=digest(selection.configuration),
        )
    )
    db.flush()


def connection_for_job(
    db: Session, job: GenerationJob, settings: Settings, *, for_output: bool = False
) -> MediaConnection | None:
    selection = selection_for_job(db, job)
    if selection is None:
        return None
    if not for_output:
        from arclight.application.commands import require_project
        from arclight.persistence.models import User

        require_project(db, job.project_id, selection.created_by, write=True)
        user = db.get(User, UUID(selection.created_by))
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "原媒体凭证所属账号已停用", 403)
    return MediaConnection(
        selection.configuration,
        ""
        if for_output
        else read_credential(db, settings, {"credential_id": str(selection.credential_id)}),
    )


def public_configuration(db: Session, actor: str, task_type: str) -> dict:
    selection = account_selection(db, actor, task_type)
    if selection is not None:
        from arclight.providers.media_capabilities import resolve_parameters
        from arclight.providers.native_media_protocols import capabilities

        configuration = selection.configuration
        priced = configuration["maximum_cost_per_output"] is not None and bool(
            configuration["pricing_version"]
        )
        return {
            **configuration,
            "configured": True,
            "enabled": priced,
            "supported_parameters": sorted(
                resolve_parameters(configuration["provider"], configuration["model"])
            ),
            "credential_source": "ACCOUNT_SNAPSHOT",
            "capabilities": capabilities(configuration["provider"]).model_dump(mode="json"),
            "message": "已配置"
            if priced
            else "账号配置已保存；生成前需要部署侧登记该精确模型的可信报价",
        }
    settings = db.info.get("settings")
    if task_type == "IMAGE" and settings is not None:
        return {
            "provider": "openai",
            "enabled": settings.image_service.enabled,
            "credential_source": "SERVER",
            **settings.image_service.public_configuration(),
        }
    return {"configured": False, "enabled": False, "message": "请先配置该类型的账号服务"}
