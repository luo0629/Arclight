"""Dialogue production over the existing generation queue and private artifact store."""

import hashlib
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.asset_mutations import change_asset
from arclight.application.asset_objects import object_key, write_object
from arclight.application.audio_production import (
    approved_script,
    bind_audio,
    capture_line_inputs,
    input_context,
    line_target,
    require_line,
    require_snapshot,
    write_episode,
)
from arclight.application.audio_voices import require_voice, voice_spec
from arclight.application.audit import audit
from arclight.application.dependencies import attach_output
from arclight.application.files import record_asset_file
from arclight.application.generation_attempts import require_writable_target
from arclight.application.generation_jobs import create_job
from arclight.application.invalidation import input_expired
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_production import AudioBindingRead, AudioInputCreate, AudioValidationRead
from arclight.domain.audio_tts import (
    AudioTTSCreate,
    AudioTTSRead,
    AudioTTSRegenerate,
    AudioTTSRegister,
    AudioTTSResultRead,
)
from arclight.domain.dependencies import DependencyInput
from arclight.domain.file_policy import POLICIES
from arclight.domain.generation_requests import ExpectedAsset, GenerationCreate
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    FileRegistration,
    GenerationAttempt,
    GenerationJob,
    GenerationMediaArtifact,
    StoredFile,
    VisualAsset,
)


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def origin_for(row: AudioTTSRequest) -> dict:
    origin = {
        "schema_version": "audio-tts/1",
        "snapshot_id": str(row.snapshot_id),
        "line_id": row.line_id,
        "voice_version_id": str(row.voice_version_id),
        "dependency_manifest_id": str(row.dependency_manifest_id),
        "request_sha256": row.request_fingerprint,
    }
    if row.source_audio_version_id is not None:
        origin["source_audio_version_id"] = str(row.source_audio_version_id)
    return origin


def result_version(db: Session, row: AudioTTSRequest) -> AssetVersion | None:
    return db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == row.target_asset_id,
            AssetVersion.creation_key == uuid5(row.job_id, "audio-tts-result"),
        )
    )


def require_tts(
    db: Session, snapshot_id: UUID, line_id: str, job_id: UUID, actor: str, *, write=False
) -> AudioTTSRequest:
    require_snapshot(db, snapshot_id, actor, write=write)
    row = db.scalar(select(AudioTTSRequest).where(AudioTTSRequest.job_id == job_id))
    if row is None or row.snapshot_id != snapshot_id or row.line_id != line_id:
        raise DomainError("NOT_FOUND", "该台词的配音任务不存在", 404)
    return row


def read_tts(db: Session, row: AudioTTSRequest) -> AudioTTSRead:
    job = db.get(GenerationJob, row.job_id, populate_existing=True)
    assert job is not None
    version = result_version(db, row)
    return AudioTTSRead(
        job_id=job.id,
        snapshot_id=row.snapshot_id,
        line_id=row.line_id,
        voice_version_id=row.voice_version_id,
        target_asset_id=row.target_asset_id,
        dependency_manifest_id=row.dependency_manifest_id,
        generation_manifest_id=job.input_manifest_id,
        status=job.status,
        revision=job.revision,
        error_code=db.scalar(
            select(GenerationAttempt.error_code)
            .where(GenerationAttempt.job_id == job.id)
            .order_by(GenerationAttempt.attempt_no.desc())
            .limit(1)
        ),
        asset_version_id=version.id if version else None,
        source_audio_version_id=row.source_audio_version_id,
        created_at=row.created_at,
    )


def require_regeneration_source(
    db: Session, project_id: UUID, snapshot_id: UUID, line_id: str, version_id: UUID
) -> VisualAsset:
    """A regeneration stays on the exact bound line and original audio asset."""
    binding = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == version_id)
    )
    version = db.get(AssetVersion, version_id)
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True) if version else None
    if (
        binding is None
        or binding.snapshot_id != snapshot_id
        or binding.line_id != line_id
        or asset is None
        or asset.project_id != project_id
        or asset.asset_type != "AUDIO"
    ):
        raise DomainError(
            "AUDIO_REGENERATION_SOURCE_INVALID", "重新生成须指定本快照本句已绑定的精确音频版本", 409
        )
    return asset


def create_tts(
    db: Session,
    snapshot_id: UUID,
    line_id: str,
    actor: str,
    payload: AudioTTSCreate,
    *,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    source_audio_version_id: UUID | None = None,
) -> tuple[AudioTTSRead, bool]:
    snapshot = require_snapshot(db, snapshot_id, actor, write=True)
    line = require_line(snapshot, line_id)
    episode = write_episode(db, snapshot.episode_id, actor)
    request_hash = fingerprint(
        {
            **payload.model_dump(mode="json", exclude={"expected_revision"}),
            **(
                {"source_audio_version_id": str(source_audio_version_id)}
                if source_audio_version_id is not None
                else {}
            ),
            "snapshot_id": str(snapshot_id),
            "line_id": line_id,
            "actor": actor,
        }
    )
    previous = db.scalar(
        select(AudioTTSRequest).where(
            AudioTTSRequest.project_id == episode.project_id,
            AudioTTSRequest.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_fingerprint != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一配音标识不能更换台词或音色配置", 409)
        return read_tts(db, previous), False
    voice = require_voice(db, episode.project_id, payload.voice_version_id, actor, write=True)
    if voice.speaker != line.speaker:
        raise DomainError("AUDIO_VOICE_SPEAKER_MISMATCH", "音色版本不属于该台词的说话角色", 422)
    if source_audio_version_id is None:
        asset = line_target(db, snapshot, line_id, payload.target_asset_id, actor)
    else:
        asset = require_regeneration_source(
            db, episode.project_id, snapshot.id, line_id, source_audio_version_id
        )
        if payload.target_asset_id is not None and payload.target_asset_id != asset.id:
            raise DomainError(
                "AUDIO_REGENERATION_SOURCE_INVALID", "重新生成不能更换原音频资产", 409
            )
    if asset.status in {"ARCHIVED", "LOCKED", "STALE", "REJECTED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档、锁定或失效资产不能生成新音频", 409)
    manifest = capture_line_inputs(
        db,
        snapshot.id,
        line_id,
        actor,
        AudioInputCreate(creation_key=payload.creation_key, target_asset_id=asset.id),
    )
    request = AudioTTSRequest(
        project_id=episode.project_id,
        snapshot_id=snapshot.id,
        line_id=line_id,
        voice_version_id=voice.id,
        target_asset_id=asset.id,
        source_audio_version_id=source_audio_version_id,
        dependency_manifest_id=manifest.id,
        creation_key=payload.creation_key,
        request_fingerprint=request_hash,
        created_by=actor,
    )
    spec = voice_spec(voice, line.text, payload.performance_parameters)
    job = create_job(
        db,
        episode.project_id,
        actor,
        f"audio-tts:{payload.creation_key}",
        GenerationCreate(
            episode_id=episode.id,
            batch_id=payload.batch_id,
            scheduling_mode=payload.scheduling_mode,
            target_type="AUDIO",
            target_asset_id=asset.id,
            expected_asset=ExpectedAsset(
                revision=payload.expected_revision,
                lock_version=asset.lock_version,
                current_version_id=asset.current_version_id,
            ),
            task_type="AUDIO",
            inputs=[
                DependencyInput(
                    source_asset_version_id=snapshot.script_version_id,
                    dependency_type="CONTENT_DEPENDENCY",
                    selectors=[f"dialogue:{line_id}"],
                )
            ],
            spec=spec,
        ),
        execution_policy=execution_policy,
        frozen_service=voice.service_snapshot_json,
        audio_line_origin=origin_for(request),
    )
    request.job_id = job.id
    db.add(request)
    db.flush()
    db.refresh(request)
    audit(db, episode.project_id, actor, "audio.tts_created", job.id, origin_for(request))
    return read_tts(db, request), True


def regenerate_tts(
    db: Session,
    snapshot_id: UUID,
    line_id: str,
    actor: str,
    payload: AudioTTSRegenerate,
    *,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
) -> tuple[AudioTTSRead, bool]:
    return create_tts(
        db,
        snapshot_id,
        line_id,
        actor,
        AudioTTSCreate(**payload.model_dump(exclude={"source_audio_version_id"})),
        execution_policy=execution_policy,
        source_audio_version_id=payload.source_audio_version_id,
    )


def validate_tts_dispatch(db: Session, job: GenerationJob) -> None:
    """Revalidate dialogue before a new paid attempt, retaining generic media behavior."""
    generation = db.get(DependencyManifest, job.input_manifest_id)
    origin = generation.snapshot_json["context"].get("audio_line") if generation else None
    row = db.scalar(select(AudioTTSRequest).where(AudioTTSRequest.job_id == job.id))
    if origin is None and row is None:
        return
    if row is None or origin != origin_for(row):
        raise DomainError("AUDIO_PROVENANCE_REQUIRED", "配音任务缺少匹配的精确台词来源", 409)
    snapshot = require_snapshot(db, row.snapshot_id, job.created_by, write=True)
    line = require_line(snapshot, row.line_id)
    episode = write_episode(db, snapshot.episode_id, job.created_by)
    approved_script(db, episode, snapshot.script_version_id, job.created_by)
    voice = require_voice(db, row.project_id, row.voice_version_id, job.created_by, write=True)
    if row.source_audio_version_id is not None:
        source_asset = require_regeneration_source(
            db, row.project_id, snapshot.id, row.line_id, row.source_audio_version_id
        )
        if source_asset.id != row.target_asset_id:
            raise DomainError("AUDIO_PROVENANCE_REQUIRED", "重新生成的来源音频资产不匹配", 409)
    manifest = db.get(DependencyManifest, row.dependency_manifest_id)
    if (
        row.project_id != job.project_id
        or job.target_asset_id != row.target_asset_id
        or job.task_type != "AUDIO"
        or voice.speaker != line.speaker
        or manifest is None
        or manifest.target_asset_id != row.target_asset_id
        or manifest.snapshot_json["context"] != input_context(snapshot, row.line_id)
        or len(manifest.snapshot_json["inputs"]) != 1
        or manifest.snapshot_json["inputs"][0]["source_asset_version_id"]
        != str(snapshot.script_version_id)
        or manifest.snapshot_json["inputs"][0]["selectors"] != [f"dialogue:{row.line_id}"]
    ):
        raise DomainError("AUDIO_PROVENANCE_REQUIRED", "配音任务的台词或音色来源不匹配", 409)
    if input_expired(db, row.project_id, manifest.snapshot_json["inputs"][0], "AUDIO"):
        raise DomainError("AUDIO_LINE_NOT_USABLE", "该台词已被批准修改，不能继续配音", 409)


def read_result(db: Session, row: AudioTTSRequest) -> AudioTTSResultRead:
    version = result_version(db, row)
    if version is None:
        raise DomainError("NOT_FOUND", "该配音结果尚未登记", 404)
    binding = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == version.id)
    )
    if binding is None or binding.snapshot_id != row.snapshot_id or binding.line_id != row.line_id:
        raise DomainError("AUDIO_PROVENANCE_REQUIRED", "配音结果缺少精确台词绑定", 409)
    receipt = version.metadata_json["audio_tts"]
    return AudioTTSResultRead(
        job_id=row.job_id,
        attempt_id=receipt["attempt_id"],
        source_file_id=receipt["source_file_id"],
        voice_version_id=row.voice_version_id,
        binding=AudioBindingRead.model_validate(binding),
        validation=receipt["validation"],
        asset_revision=receipt["asset_revision"],
    )


def register_result(
    db: Session,
    settings: Settings,
    row: AudioTTSRequest,
    actor: str,
    payload: AudioTTSRegister,
) -> tuple[AudioTTSResultRead, bool]:
    snapshot = require_snapshot(db, row.snapshot_id, actor, write=True)
    write_episode(db, snapshot.episode_id, actor)
    declaration_hash = fingerprint(
        payload.model_dump(mode="json", exclude={"expected_revision"}) | {"actor": actor}
    )
    prior = result_version(db, row)
    if prior is not None:
        if prior.metadata_json["audio_tts"]["declaration_sha256"] != declaration_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "已登记配音不能更换来源授权说明", 409)
        return read_result(db, row), False
    job = db.get(GenerationJob, row.job_id, populate_existing=True)
    assert job is not None
    validate_tts_dispatch(db, job)
    require_writable_target(db, job)
    if job.status != "SUCCEEDED":
        raise DomainError("AUDIO_RESULT_NOT_READY", "配音任务尚未成功，不能登记结果", 409)
    artifacts = list(
        db.scalars(
            select(GenerationMediaArtifact)
            .join(GenerationAttempt, GenerationAttempt.id == GenerationMediaArtifact.attempt_id)
            .where(
                GenerationMediaArtifact.job_id == job.id,
                GenerationAttempt.status == "SUCCEEDED",
            )
        )
    )
    if len(artifacts) != 1 or artifacts[0].error_code or artifacts[0].file_id is None:
        raise DomainError("AUDIO_RESULT_NOT_READY", "配音结果尚未归档或机器检查未通过", 409)
    artifact = artifacts[0]
    source = db.get(StoredFile, artifact.file_id)
    if (
        source is None
        or source.project_id != row.project_id
        or source.asset_version_id is not None
        or not source.media_type.startswith("audio/")
        or source.media_type not in POLICIES
    ):
        raise DomainError("AUDIO_SOURCE_INVALID", "配音原始文件回执不匹配", 409)
    data = file_storage(settings, source).read(
        source.object_key, POLICIES[source.media_type].max_bytes, source.checksum
    )
    if len(data) != source.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "配音大小与不可变文件回执不符", 409)
    validation = AudioValidationRead.model_validate(probe(data, source.media_type))
    generation_manifest = db.get(DependencyManifest, job.input_manifest_id)
    assert generation_manifest is not None
    output = generation_manifest.snapshot_json["context"]["generation"]["output_spec"]
    expected_format = output.get("format", "wav")
    expected_type = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
    }[expected_format]
    if (
        validation.media_type != expected_type
        or validation.channels != output.get("channels", 1)
        or ("sample_rate" in output and validation.sample_rate != output["sample_rate"])
        or (expected_format == "opus" and validation.codec != "opus")
    ):
        raise DomainError(
            "AUDIO_OUTPUT_MISMATCH", "配音实测格式、采样率或声道与冻结输出规格不符", 422
        )
    if validation.duration_ms <= 0 or validation.silence.silent:
        raise DomainError("AUDIO_SILENT", "配音结果没有有效语音，不能作为台词音频登记", 422)
    asset = db.get(VisualAsset, row.target_asset_id, populate_existing=True)
    assert asset is not None
    change_asset(db, asset, payload.expected_revision)
    version_id = uuid4()
    version = AssetVersion(
        id=version_id,
        asset_id=asset.id,
        version_no=(
            db.scalar(
                select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
            )
            or 0
        )
        + 1,
        object_key=object_key(row.project_id, version_id, settings),
        checksum=source.checksum,
        created_by=actor,
        creation_key=uuid5(job.id, "audio-tts-result"),
        status="DRAFT",
        metadata_json={
            "storage_backend": settings.object_storage_backend.upper(),
            "dependency_manifest_id": str(row.dependency_manifest_id),
            "audio_tts": {
                **origin_for(row),
                "job_id": str(job.id),
                "generation_manifest_id": str(job.input_manifest_id),
                "attempt_id": str(artifact.attempt_id),
                "source_file_id": str(source.id),
                "declaration_sha256": declaration_hash,
                "asset_revision": asset.revision,
                "validation": validation.model_dump(mode="json"),
            },
            "media": {
                "media_type": source.media_type,
                "byte_size": source.byte_size,
                "original_name": source.original_name,
                "source_note": payload.source_note,
                "rights_note": payload.rights_note,
                "rights_confirmed": True,
            },
        },
    )
    write_object(db, settings, row.project_id, version.id, data, source.media_type)
    db.add(version)
    db.flush()
    db.refresh(version)
    record_asset_file(db, settings, row.project_id, version)
    db.add(FileRegistration(file_id=source.id, asset_version_id=version.id, created_by=actor))
    attach_output(db, row.project_id, row.dependency_manifest_id, version)
    bind_audio(db, settings, row.snapshot_id, row.line_id, version.id, actor)
    audit(db, row.project_id, actor, "audio.tts_registered", version.id, {"job_id": str(job.id)})
    return read_result(db, row), True
