"""Versioned voice choices reuse the existing native media service snapshot."""

import hashlib
import json
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.media_services import account_selection
from arclight.core.errors import DomainError
from arclight.domain.audio_voices import AudioVoiceCreate, AudioVoiceRead
from arclight.domain.generation_requests import GenerationSpec
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import VisualAsset
from arclight.providers.native_media_protocols import validate_spec


def read_voice(row: AudioVoiceVersion) -> AudioVoiceRead:
    return AudioVoiceRead(
        id=row.id,
        project_id=row.project_id,
        speaker=row.speaker,
        character_asset_id=row.character_asset_id,
        version_no=row.version_no,
        created_by=row.created_by,
        created_at=row.created_at,
        **row.configuration_json,
    )


def require_voice(
    db: Session,
    project_id: UUID,
    voice_id: UUID,
    actor: str,
    *,
    write: bool = False,
) -> AudioVoiceVersion:
    require_project(db, project_id, actor, write=write)
    row = db.get(AudioVoiceVersion, voice_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "音色版本不存在", 404)
    return row


def voice_spec(
    row: AudioVoiceVersion,
    text: str,
    performance_parameters: dict[str, JsonValue] | None = None,
) -> GenerationSpec:
    performance = performance_parameters or {}
    if set(performance) - {"speed", "instructions"}:
        raise DomainError(
            "AUDIO_PERFORMANCE_UNSUPPORTED", "逐句表演只能调整协议支持的语速或情绪说明", 422
        )
    configuration = row.configuration_json
    voice = configuration["parameters"].get("voice")
    if isinstance(voice, str) and not voice.strip():
        raise DomainError("PROVIDER_VOICE_REQUIRED", "语音任务需要选择非空白音色", 422)
    spec = GenerationSpec(
        provider=configuration["provider"],
        model=configuration["model"],
        model_config_version=configuration["model_config_version"],
        prompt=text,
        prompt_version="audio-line/1",
        parameters={**configuration["parameters"], **performance},
        output_spec=dict(configuration["output_spec"]),
        context={},
    )
    validate_spec(spec)
    return spec


def create_voice_version(
    db: Session, project_id: UUID, actor: str, payload: AudioVoiceCreate
) -> tuple[AudioVoiceVersion, bool]:
    require_project(db, project_id, actor, write=True)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema_version": "audio-voice/1",
                "project_id": str(project_id),
                "actor": actor,
                **payload.model_dump(mode="json", exclude={"expected_version_no"}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    previous = db.scalar(
        select(AudioVoiceVersion).where(
            AudioVoiceVersion.project_id == project_id,
            AudioVoiceVersion.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一音色创建标识不能更换说话者或配置", 409)
        return previous, False
    current = (
        db.scalar(
            select(func.max(AudioVoiceVersion.version_no)).where(
                AudioVoiceVersion.project_id == project_id,
                AudioVoiceVersion.speaker == payload.speaker,
            )
        )
        or 0
    )
    if current != payload.expected_version_no:
        raise DomainError("AUDIO_VOICE_CONFLICT", "音色默认版本已变化，请重新读取后保存", 409)
    if payload.character_asset_id is not None:
        character = db.get(VisualAsset, payload.character_asset_id, populate_existing=True)
        if character is None or character.project_id != project_id:
            raise DomainError("NOT_FOUND", "角色资产不存在", 404)
        if character.asset_type != "CHARACTER":
            raise DomainError("AUDIO_CHARACTER_INVALID", "音色只能关联角色资产", 422)
        if character.status in {"ARCHIVED", "REJECTED", "STALE"}:
            raise DomainError("ASSET_NOT_USABLE", "已归档、拒绝或失效角色不能创建音色版本", 409)
    selection = account_selection(db, actor, "AUDIO")
    if selection is None:
        raise DomainError("SERVICE_INCOMPLETE", "请先在账号设置中配置语音服务", 422)
    provider_configuration = selection.configuration
    row = AudioVoiceVersion(
        project_id=project_id,
        speaker=payload.speaker,
        character_asset_id=payload.character_asset_id,
        version_no=current + 1,
        creation_key=payload.creation_key,
        request_fingerprint=fingerprint,
        configuration_json={
            "schema_version": "audio-voice/1",
            "provider": provider_configuration["provider"],
            "model": provider_configuration["model"],
            "model_config_version": provider_configuration["configuration_version"],
            "parameters": dict(payload.parameters),
            "output_spec": dict(payload.output_spec),
            "provider_configuration": provider_configuration,
        },
        service_snapshot_json=selection.private_snapshot(),
        created_by=actor,
    )
    # The actual line text is supplied later by the approved server-side snapshot.
    voice_spec(row, "音色配置校验")
    db.add(row)
    db.flush()
    db.refresh(row)
    audit(
        db,
        project_id,
        actor,
        "audio.voice.created",
        row.id,
        {"speaker": row.speaker, "version_no": row.version_no},
    )
    return row, True
