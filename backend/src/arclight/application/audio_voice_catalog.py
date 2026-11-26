"""Read voice choices for an exact account route and verify catalog membership."""

from dataclasses import dataclass, field
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.media_services import (
    MediaSelection,
    account_selection,
    credential_for_selection,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_voices import AudioVoiceCatalogRead, AudioVoiceOption
from arclight.domain.model_gateway import GatewayFailure
from arclight.domain.service_models import matching_policy
from arclight.providers.media_capabilities import parameter_ranges, resolve_parameters
from arclight.providers.media_io import bounded_call
from arclight.providers.speech_catalogs import VoiceCatalogDefinition, official_voice_catalog


@dataclass(frozen=True)
class VoiceCatalogSource:
    selection: MediaSelection | None = None
    definition: VoiceCatalogDefinition | None = None
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 0
    dns_timeout: float = 0
    error_code: str | None = None


def read_voice_catalog_source(
    db: Session,
    settings: Settings,
    project_id: UUID,
    actor: str,
    *,
    selection: MediaSelection | None = None,
) -> VoiceCatalogSource:
    require_project(db, project_id, actor, write=True)
    if selection is not None and selection.created_by != actor:
        raise DomainError("FORBIDDEN", "语音配置不属于当前账号", 403)
    try:
        if selection is None:
            selection = account_selection(db, actor, "AUDIO")
        if selection is None:
            return VoiceCatalogSource()
        configuration = selection.configuration
        policy = matching_policy(settings.service_model_policies, configuration)
        definition = (
            VoiceCatalogDefinition(policy.speech_voice_options)
            if policy and policy.speech_voice_options
            else official_voice_catalog(configuration)
        )
        if definition is None:
            return VoiceCatalogSource(selection)
        key = credential_for_selection(db, settings, project_id, selection)
    except DomainError as error:
        return VoiceCatalogSource(selection, error_code=error.code)
    return VoiceCatalogSource(
        selection,
        definition,
        key if definition.query is not None else None,
        min(10, settings.media_execution_policy.call_timeout_seconds),
        settings.llm_dns_timeout_seconds,
    )


def query_voice_catalog(source: VoiceCatalogSource) -> AudioVoiceCatalogRead:
    selection = source.selection
    if selection is None:
        return AudioVoiceCatalogRead(
            status="QUERY_FAILED" if source.error_code else "NOT_CONFIGURED",
            error_code=source.error_code,
        )
    configuration = selection.configuration
    provider, model = configuration["provider"], configuration["model"]
    catalog = {
        "supplier": configuration["supplier"],
        "model": model,
        "configuration_version": configuration["configuration_version"],
        "supported_parameters": tuple(sorted(resolve_parameters(provider, model))),
        "parameter_ranges": parameter_ranges(provider, model),
    }
    if source.error_code:
        return AudioVoiceCatalogRead(status="QUERY_FAILED", error_code=source.error_code, **catalog)
    if source.definition is None:
        return AudioVoiceCatalogRead(status="UNAVAILABLE", **catalog)
    options = source.definition.options
    query = source.definition.query
    if query is not None:
        key = source.api_key
        assert key is not None
        try:
            options += bounded_call(
                lambda: query(configuration, key, source.timeout, source.dns_timeout),
                source.timeout,
            )
        except (DomainError, GatewayFailure) as error:
            return AudioVoiceCatalogRead(status="QUERY_FAILED", error_code=error.code, **catalog)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return AudioVoiceCatalogRead(
                status="QUERY_FAILED", error_code="AUDIO_VOICE_CATALOG_QUERY_FAILED", **catalog
            )
    options_by_id: dict[str, AudioVoiceOption] = {}
    for option in options:
        options_by_id.setdefault(option.id, option)
    return AudioVoiceCatalogRead(
        status="READY",
        options=tuple(options_by_id.values()),
        **catalog,
    )


def require_catalog_voice(catalog: AudioVoiceCatalogRead, voice: object) -> None:
    if catalog.status == "NOT_CONFIGURED":
        raise DomainError("SERVICE_INCOMPLETE", "请先在模型配置中保存语音服务", 422)
    if catalog.status == "UNAVAILABLE":
        raise DomainError("AUDIO_VOICE_CATALOG_UNAVAILABLE", "该语音模型未提供可用音色目录", 409)
    if catalog.status == "QUERY_FAILED":
        raise DomainError(
            "AUDIO_VOICE_CATALOG_QUERY_FAILED", "音色目录查询失败，请检查语音服务后重新加载", 409
        )
    if not isinstance(voice, str) or voice not in {option.id for option in catalog.options}:
        raise DomainError("AUDIO_VOICE_NOT_IN_CATALOG", "请选择当前语音模型目录中存在的音色", 422)
