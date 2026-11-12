"""Import a frozen uploaded recording as an exact, traceable dialogue version."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.asset_mutations import change_asset
from arclight.application.asset_objects import object_key, write_object
from arclight.application.audio_production import (
    bind_audio,
    capture_line_inputs,
    line_target,
    require_line,
    require_snapshot,
    write_episode,
)
from arclight.application.audit import audit
from arclight.application.dependencies import attach_output
from arclight.application.files import record_asset_file
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_production import (
    AudioImportCreate,
    AudioImportRead,
    AudioInputCreate,
    AudioValidationRead,
)
from arclight.domain.file_policy import check_file_policy
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.models import (
    AssetVersion,
    FileRegistration,
    StoredFile,
    UploadCompletion,
)


def read_import(db: Session, snapshot_id: UUID, line_id: str, version_id: UUID) -> AudioImportRead:
    binding = db.scalar(
        select(AudioLineBinding).where(
            AudioLineBinding.asset_version_id == version_id,
            AudioLineBinding.snapshot_id == snapshot_id,
            AudioLineBinding.line_id == line_id,
        )
    )
    version = db.get(AssetVersion, version_id)
    declaration = version.metadata_json.get("audio_import") if version else None
    if binding is None or version is None or not isinstance(declaration, dict):
        raise DomainError("NOT_FOUND", "该台词的音频导入记录不存在", 404)
    return AudioImportRead(
        snapshot_id=snapshot_id,
        line_id=line_id,
        target_asset_id=version.asset_id,
        asset_revision=declaration["asset_revision"],
        asset_version_id=version.id,
        file_id=binding.file_id,
        source_file_id=declaration["source_file_id"],
        dependency_manifest_id=binding.dependency_manifest_id,
        binding_id=binding.id,
        validation=declaration["validation"],
        created_by=version.created_by,
        created_at=version.created_at,
    )


def source_audio(
    db: Session, settings: Settings, project_id: UUID, file_id: UUID
) -> tuple[StoredFile, bytes, AudioValidationRead]:
    source = db.get(StoredFile, file_id)
    if source is None or source.project_id != project_id:
        raise DomainError("NOT_FOUND", "音频来源文件不存在", 404)
    completion = db.scalar(select(UploadCompletion).where(UploadCompletion.file_id == source.id))
    if (
        completion is None
        or completion.purpose != "AUDIO"
        or source.asset_version_id is not None
        or not source.media_type.startswith("audio/")
        or not source.rights_confirmed
        or not source.source_note.strip()
        or not source.rights_note.strip()
    ):
        raise DomainError("AUDIO_SOURCE_INVALID", "须使用已完成上传并确认来源和授权的音频", 422)
    policy = check_file_policy("AUDIO", source.original_name, source.media_type, source.byte_size)
    data = file_storage(settings, source).read(source.object_key, policy.max_bytes, source.checksum)
    if len(data) != source.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "音频大小与不可变上传回执不符", 409)
    details = probe(data, source.media_type)
    if details["duration_ms"] <= 0:
        raise DomainError("INVALID_FILE", "音频实测时长须至少为一毫秒", 422)
    validation = AudioValidationRead.model_validate(details)
    if validation.silence.silent:
        raise DomainError("AUDIO_SILENT", "录音为整段静音，请上传有效语音或单独确认静音决定", 422)
    return source, data, validation


def import_audio(
    db: Session,
    settings: Settings,
    snapshot_id: UUID,
    line_id: str,
    actor: str,
    payload: AudioImportCreate,
) -> tuple[AudioImportRead, bool]:
    snapshot = require_snapshot(db, snapshot_id, actor, write=True)
    require_line(snapshot, line_id)
    # Acquire the same project lock as text invalidation before idempotency/CAS checks.
    episode = write_episode(db, snapshot.episode_id, actor)
    asset = line_target(db, snapshot, line_id, payload.target_asset_id, actor)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema_version": "audio-import/1",
                "snapshot_id": str(snapshot.id),
                "line_id": line_id,
                "target_asset_id": str(asset.id),
                "source_file_id": str(payload.source_file_id),
                "actor": actor,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    previous = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset.id, AssetVersion.creation_key == payload.creation_key
        )
    )
    if previous is not None:
        if previous.metadata_json.get("request_sha256") != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一导入标识不能更换音频来源或台词", 409)
        return read_import(db, snapshot.id, line_id, previous.id), False
    if asset.status in {"ARCHIVED", "LOCKED", "STALE", "REJECTED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档、锁定或失效资产不能导入新音频", 409)
    manifest = capture_line_inputs(
        db,
        snapshot.id,
        line_id,
        actor,
        AudioInputCreate(creation_key=payload.creation_key, target_asset_id=asset.id),
    )
    source, data, validation = source_audio(
        db, settings, episode.project_id, payload.source_file_id
    )
    change_asset(db, asset, payload.expected_revision)
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
        )
        or 0
    ) + 1
    version_id = uuid4()
    version = AssetVersion(
        id=version_id,
        asset_id=asset.id,
        version_no=number,
        object_key=object_key(episode.project_id, version_id, settings),
        checksum=source.checksum,
        created_by=actor,
        creation_key=payload.creation_key,
        status="DRAFT",
        metadata_json={
            "storage_backend": settings.object_storage_backend.upper(),
            "request_sha256": fingerprint,
            "dependency_manifest_id": str(manifest.id),
            "audio_import": {
                "schema_version": "audio-import/1",
                "source_file_id": str(source.id),
                "asset_revision": asset.revision,
                "validation": validation.model_dump(mode="json"),
            },
            "media": {
                "media_type": source.media_type,
                "byte_size": source.byte_size,
                "original_name": source.original_name,
                "source_note": source.source_note,
                "rights_note": source.rights_note,
                "rights_confirmed": source.rights_confirmed,
            },
        },
    )
    write_object(db, settings, episode.project_id, version.id, data, source.media_type)
    db.add(version)
    db.flush()
    db.refresh(version)
    record_asset_file(db, settings, episode.project_id, version)
    db.add(FileRegistration(file_id=source.id, asset_version_id=version.id, created_by=actor))
    attach_output(db, episode.project_id, manifest.id, version)
    bind_audio(db, settings, snapshot.id, line_id, version.id, actor)
    audit(
        db,
        episode.project_id,
        actor,
        "audio.imported",
        version.id,
        {"source_file_id": str(source.id), "snapshot_id": str(snapshot.id), "line_id": line_id},
    )
    return read_import(db, snapshot.id, line_id, version.id), True
