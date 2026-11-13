"""Register licensed music and effects through the existing asset/file lifecycle."""

import hashlib
import json
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.asset_mutations import change_asset
from arclight.application.asset_objects import object_key, write_object
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.files import record_asset_file
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_production import AudioValidationRead
from arclight.domain.file_policy import check_file_policy
from arclight.domain.music_assets import MusicImportCreate, MusicPurposeRole, MusicResource
from arclight.media.validation import probe
from arclight.persistence.models import (
    AssetVersion,
    FileRegistration,
    StoredFile,
    UploadCompletion,
    UploadIntent,
    VisualAsset,
)


def music_source_facts(db: Session, project_id: UUID, file_id: UUID) -> StoredFile:
    source = db.get(StoredFile, file_id)
    if source is None or source.project_id != project_id:
        raise DomainError("NOT_FOUND", "音乐或音效来源文件不存在", 404)
    completion = db.scalar(select(UploadCompletion).where(UploadCompletion.file_id == source.id))
    upload = db.get(UploadIntent, completion.upload_id) if completion is not None else None
    declaration = {
        "source_note": source.source_note,
        "rights_note": source.rights_note,
        "rights_confirmed": source.rights_confirmed,
    }
    declaration_sha256 = hashlib.sha256(
        json.dumps(declaration, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if (
        completion is None
        or completion.purpose != "MUSIC"
        or completion.request_sha256 != declaration_sha256
        or upload is None
        or upload.project_id != project_id
        or upload.purpose != "MUSIC"
        or upload.checksum != source.checksum
        or upload.byte_size != source.byte_size
        or upload.declared_media_type != source.media_type
        or upload.original_name != source.original_name
        or source.storage_backend not in {"LOCAL", "S3"}
        or source.asset_version_id is not None
        or not source.rights_confirmed
        or not source.source_note.strip()
        or not source.rights_note.strip()
    ):
        raise DomainError(
            "MUSIC_SOURCE_INVALID", "须使用已完成上传并确认来源与授权的音乐或音效", 422
        )
    check_file_policy("MUSIC", source.original_name, source.media_type, source.byte_size)
    return source


def music_request_sha256(
    asset_id: UUID, file_id: UUID, purpose_role: MusicPurposeRole, actor: str
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "schema_version": "audio-resource/1",
                "asset_id": str(asset_id),
                "source_file_id": str(file_id),
                "purpose_role": purpose_role,
                "actor": actor,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def music_resource_facts(
    db: Session, project_id: UUID, asset: VisualAsset, version: AssetVersion
) -> MusicResource:
    """Verify the exact frozen receipt chain without accessing objects or parsers."""
    if (
        asset.project_id != project_id
        or asset.asset_type != "MUSIC"
        or version.asset_id != asset.id
        or version.stale
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
    ):
        raise DomainError("MUSIC_VERSION_NOT_USABLE", "音乐或音效精确版本不可用于新审核或消费", 409)
    declaration = version.metadata_json.get("audio_resource")
    if not isinstance(declaration, dict) or declaration.get("schema_version") != "audio-resource/1":
        raise DomainError("MUSIC_SOURCE_INVALID", "音乐或音效缺少受支持的来源检查合同", 409)
    try:
        resource = MusicResource.model_validate(declaration)
    except ValidationError:
        raise DomainError(
            "MUSIC_SOURCE_INVALID", "音乐或音效缺少有效的不可变来源与检查记录", 409
        ) from None
    source = music_source_facts(db, project_id, resource.source_file_id)
    registration = db.scalar(
        select(FileRegistration).where(FileRegistration.asset_version_id == version.id)
    )
    output = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    media = version.metadata_json.get("media")
    if (
        registration is None
        or registration.file_id != source.id
        or registration.created_by != version.created_by
        or version.creation_key is None
        or version.metadata_json.get("request_sha256")
        != music_request_sha256(asset.id, source.id, resource.purpose_role, version.created_by)
        or output is None
        or output.project_id != project_id
        or output.checksum != source.checksum
        or output.checksum != version.checksum
        or output.object_key != version.object_key
        or output.storage_backend not in {"LOCAL", "S3"}
        or output.storage_backend != version.metadata_json.get("storage_backend")
        or output.created_by != version.created_by
        or output.byte_size != source.byte_size
        or output.media_type != source.media_type
        or output.original_name != source.original_name
        or output.source_note != source.source_note
        or output.rights_note != source.rights_note
        or output.rights_confirmed is not True
        or not isinstance(media, dict)
        or media
        != {
            "media_type": output.media_type,
            "byte_size": output.byte_size,
            "original_name": output.original_name,
            "source_note": output.source_note,
            "rights_note": output.rights_note,
            "rights_confirmed": output.rights_confirmed,
        }
        or resource.validation.media_type != output.media_type
        or resource.validation.silence.silent
    ):
        raise DomainError(
            "MUSIC_SOURCE_INVALID", "音乐或音效与冻结文件、来源授权和检查记录不符", 409
        )
    return resource


def require_music_version(
    db: Session,
    settings: Settings,
    project_id: UUID,
    asset: VisualAsset,
    version: AssetVersion,
    actor: str,
) -> MusicResource:
    require_project(db, project_id, actor)
    resource = music_resource_facts(db, project_id, asset, version)
    output = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    assert output is not None
    policy = check_file_policy("MUSIC", output.original_name, output.media_type, output.byte_size)
    data = file_storage(settings, output).read(output.object_key, policy.max_bytes, output.checksum)
    if len(data) != output.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "音乐或音效大小与不可变文件回执不符", 409)
    validation = AudioValidationRead.model_validate(probe(data, output.media_type))
    if validation != resource.validation or validation.silence.silent:
        raise DomainError("MUSIC_OUTPUT_MISMATCH", "音乐或音效实测结果与冻结检查记录不符", 422)
    return resource


def register_music(
    db: Session,
    settings: Settings,
    project_id: UUID,
    file_id: UUID,
    actor: str,
    payload: MusicImportCreate,
) -> AssetVersion:
    require_project(db, project_id, actor, write=True)
    asset = db.get(VisualAsset, payload.asset_id, populate_existing=True)
    if asset is None or asset.project_id != project_id or asset.asset_type != "MUSIC":
        raise DomainError("NOT_FOUND", "音乐或音效资产不存在", 404)
    fingerprint = music_request_sha256(asset.id, file_id, payload.purpose_role, actor)
    previous = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset.id, AssetVersion.creation_key == payload.creation_key
        )
    )
    if previous is not None:
        if previous.metadata_json.get("request_sha256") != fingerprint:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT", "同一导入标识不能更换音乐或音效来源和用途", 409
            )
        return previous
    if asset.status in {"ARCHIVED", "LOCKED", "STALE", "REJECTED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档、锁定或失效资产不能导入新音乐或音效", 409)
    source = music_source_facts(db, project_id, file_id)
    policy = check_file_policy("MUSIC", source.original_name, source.media_type, source.byte_size)
    data = file_storage(settings, source).read(source.object_key, policy.max_bytes, source.checksum)
    if len(data) != source.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "音乐或音效大小与不可变上传回执不符", 409)
    validation = AudioValidationRead.model_validate(probe(data, source.media_type))
    if validation.silence.silent:
        raise DomainError("MUSIC_SILENT", "音乐或音效不能为整段静音", 422)
    change_asset(db, asset, payload.expected_revision)
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
        )
        or 0
    ) + 1
    resource = MusicResource(
        purpose_role=payload.purpose_role, source_file_id=source.id, validation=validation
    )
    version_id = uuid4()
    version = AssetVersion(
        id=version_id,
        asset_id=asset.id,
        version_no=number,
        object_key=object_key(project_id, version_id, settings),
        checksum=source.checksum,
        created_by=actor,
        creation_key=payload.creation_key,
        status="DRAFT",
        metadata_json={
            "storage_backend": settings.object_storage_backend.upper(),
            "request_sha256": fingerprint,
            "audio_resource": resource.model_dump(mode="json"),
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
    write_object(db, settings, project_id, version.id, data, source.media_type)
    db.add(version)
    db.flush()
    db.refresh(version)
    record_asset_file(db, settings, project_id, version)
    db.add(FileRegistration(file_id=source.id, asset_version_id=version.id, created_by=actor))
    audit(
        db,
        project_id,
        actor,
        "music.imported",
        version.id,
        {"source_file_id": str(source.id), "purpose_role": payload.purpose_role},
    )
    return version
