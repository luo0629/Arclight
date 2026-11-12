"""Prepare immutable dialogue inputs and register their original audio outputs."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.content_dependencies import register_content
from arclight.application.content_references import validate_script_assets
from arclight.application.dependencies import capture_manifest, exact_version, lock_dependency_graph
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_production import (
    AudioInputCreate,
    AudioLine,
    AudioSnapshotCreate,
)
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.domain.file_policy import POLICIES
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    Episode,
    Script,
    StoredFile,
    VisualAsset,
)


def write_episode(db: Session, episode_id: UUID, actor: str) -> Episode:
    episode = require_episode(db, episode_id, actor, write=True)
    lock_dependency_graph(db, episode.project_id)
    db.refresh(episode)
    return require_episode(db, episode_id, actor, write=True)


def approved_script(db: Session, episode: Episode, identity: UUID, actor: str) -> Script:
    script = db.get(Script, identity, populate_existing=True)
    if script is None or script.episode_id != episode.id:
        raise DomainError("NOT_FOUND", "本剧集的精确剧本不存在", 404)
    if script.status != "APPROVED":
        raise DomainError("AUDIO_SCRIPT_NOT_APPROVED", "音频生产需要已批准剧本", 409)
    registry = register_content(db, script, actor)
    version, asset = exact_version(db, episode.project_id, registry.id)
    checksum = hashlib.sha256(json.dumps(script.content_json, sort_keys=True).encode()).hexdigest()
    if (
        version.status not in {"APPROVED", "LOCKED"}
        or version.stale
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or version.checksum != checksum
    ):
        raise DomainError("AUDIO_SCRIPT_NOT_USABLE", "剧本来源已失效或批准内容不符", 409)
    validate_script_assets(
        db, episode.project_id, ScriptContent.model_validate(script.content_json)
    )
    return script


def capture_snapshot(
    db: Session, episode_id: UUID, actor: str, payload: AudioSnapshotCreate
) -> AudioSnapshot:
    episode = write_episode(db, episode_id, actor)
    previous = db.scalar(
        select(AudioSnapshot).where(
            AudioSnapshot.episode_id == episode_id,
            AudioSnapshot.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.script_version_id != payload.script_version_id:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一抽取键不能用于不同剧本", 409)
        return previous
    script = approved_script(db, episode, payload.script_version_id, actor)
    content = ScriptContent.model_validate(script.content_json)
    lines = [
        AudioLine(
            **line.model_dump(),
            scene_id=scene.scene_id,
            scene_no=scene.scene_no,
            sequence_no=index + 1,
        ).model_dump(mode="json")
        for scene in content.scenes
        for index, line in enumerate(scene.dialogue)
    ]
    registry = db.get(AssetVersion, script.id)
    assert registry is not None
    row = AudioSnapshot(
        episode_id=episode.id,
        script_version_id=script.id,
        script_sha256=registry.checksum,
        creation_key=payload.creation_key,
        lines_json=lines,
        line_count=len(lines),
        created_by=actor,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit(
        db,
        episode.project_id,
        actor,
        "audio.snapshot_captured",
        row.id,
        {"script_version_id": str(script.id), "line_count": len(lines)},
    )
    return row


def require_snapshot(
    db: Session, identity: UUID, actor: str, *, write: bool = False
) -> AudioSnapshot:
    row = db.get(AudioSnapshot, identity)
    if row is None:
        raise DomainError("NOT_FOUND", "音频台词快照不存在", 404)
    if write:
        write_episode(db, row.episode_id, actor)
    else:
        require_episode(db, row.episode_id, actor)
    return row


def require_line(row: AudioSnapshot, line_id: str) -> AudioLine:
    for value in row.lines_json:
        if value["line_id"] == line_id:
            return AudioLine.model_validate(value)
    raise DomainError("NOT_FOUND", "快照中没有该台词", 404)


def input_context(row: AudioSnapshot, line_id: str) -> dict:
    return {
        "audio_snapshot_id": str(row.id),
        "script_version_id": str(row.script_version_id),
        "script_sha256": row.script_sha256,
        "line_id": line_id,
    }


def capture_line_inputs(
    db: Session, snapshot_id: UUID, line_id: str, actor: str, payload: AudioInputCreate
) -> DependencyManifest:
    row = require_snapshot(db, snapshot_id, actor, write=True)
    require_line(row, line_id)
    episode = require_episode(db, row.episode_id, actor, write=True)
    approved_script(db, episode, row.script_version_id, actor)
    asset = db.get(VisualAsset, payload.target_asset_id, populate_existing=True)
    if asset is None or asset.project_id != episode.project_id:
        raise DomainError("NOT_FOUND", "音频目标不存在", 404)
    if asset.asset_type != "AUDIO":
        raise DomainError("AUDIO_TARGET_INVALID", "台词生产目标必须是音频资产", 422)
    manifest, _ = capture_manifest(
        db,
        episode.project_id,
        actor,
        ManifestCreate(
            creation_key=payload.creation_key,
            target_asset_id=asset.id,
            inputs=[
                DependencyInput(
                    source_asset_version_id=row.script_version_id,
                    dependency_type="CONTENT_DEPENDENCY",
                    selectors=[f"dialogue:{line_id}"],
                )
            ],
            context=input_context(row, line_id),
        ),
    )
    return manifest


def bind_audio(
    db: Session, settings: Settings, snapshot_id: UUID, line_id: str, identity: UUID, actor: str
) -> AudioLineBinding:
    row = require_snapshot(db, snapshot_id, actor, write=True)
    require_line(row, line_id)
    episode = require_episode(db, row.episode_id, actor, write=True)
    version, asset = exact_version(db, episode.project_id, identity)
    previous = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == identity)
    )
    if previous is not None:
        if previous.snapshot_id != row.id or previous.line_id != line_id:
            raise DomainError("AUDIO_BINDING_CONFLICT", "音频版本不能改绑台词来源", 409)
        return previous
    approved_script(db, episode, row.script_version_id, actor)
    if (
        asset.asset_type != "AUDIO"
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or version.status in {"ARCHIVED", "STALE", "REJECTED"}
        or version.stale
    ):
        raise DomainError("AUDIO_VERSION_NOT_USABLE", "音频版本已失效或类型不符", 409)
    receipt = db.scalar(
        select(DependencyOutput).where(DependencyOutput.asset_version_id == identity)
    )
    manifest = db.get(DependencyManifest, receipt.dependency_manifest_id) if receipt else None
    if (
        manifest is None
        or manifest.project_id != episode.project_id
        or manifest.target_asset_id != asset.id
        or version.metadata_json.get("dependency_manifest_id") != str(manifest.id)
        or manifest.snapshot_json["context"] != input_context(row, line_id)
        or len(manifest.snapshot_json["inputs"]) != 1
        or manifest.snapshot_json["inputs"][0]["source_asset_version_id"]
        != str(row.script_version_id)
        or manifest.snapshot_json["inputs"][0]["checksum"] != row.script_sha256
        or manifest.snapshot_json["inputs"][0]["dependency_type"] != "CONTENT_DEPENDENCY"
        or manifest.snapshot_json["inputs"][0]["selectors"] != [f"dialogue:{line_id}"]
    ):
        raise DomainError(
            "AUDIO_PROVENANCE_REQUIRED", "音频须在创建时固定本快照台词的输入清单", 409
        )
    files = list(db.scalars(select(StoredFile).where(StoredFile.asset_version_id == identity)))
    if len(files) != 1:
        raise DomainError("AUDIO_FILE_REQUIRED", "音频须有唯一的不可变文件回执", 409)
    file = files[0]
    if (
        file.project_id != episode.project_id
        or file.checksum != version.checksum
        or file.object_key != version.object_key
        or not file.media_type.startswith("audio/")
        or file.media_type not in POLICIES
        or not file.rights_confirmed
        or not file.source_note.strip()
        or not file.rights_note.strip()
    ):
        raise DomainError("AUDIO_FILE_INVALID", "音频文件及来源授权不符合要求", 409)
    data = file_storage(settings, file).read(
        file.object_key, POLICIES[file.media_type].max_bytes, file.checksum
    )
    if len(data) != file.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "音频大小与文件回执不符", 409)
    details = probe(data, file.media_type)
    binding = AudioLineBinding(
        snapshot_id=row.id,
        line_id=line_id,
        asset_version_id=identity,
        dependency_manifest_id=manifest.id,
        file_id=file.id,
        duration_ms=details["duration_ms"],
        created_by=actor,
    )
    db.add(binding)
    db.flush()
    db.refresh(binding)
    audit(
        db,
        episode.project_id,
        actor,
        "audio.version_bound",
        binding.id,
        {"snapshot_id": str(row.id), "line_id": line_id, "asset_version_id": str(identity)},
    )
    return binding


def validate_audio_line(db: Session, identity: UUID, script_id: UUID, line_id: str) -> None:
    """Enforce new production provenance, preserving pre-existing audio contracts."""
    version = db.get(AssetVersion, identity)
    if version is None:
        return
    raw = version.metadata_json.get("dependency_manifest_id")
    try:
        manifest_id = UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        manifest_id = None
    manifest = db.get(DependencyManifest, manifest_id) if manifest_id else None
    context = manifest.snapshot_json.get("context", {}) if manifest else {}
    if "audio_snapshot_id" not in context:
        return
    binding = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == identity)
    )
    if (
        binding is None
        or binding.line_id != line_id
        or context.get("script_version_id") != str(script_id)
        or context.get("audio_snapshot_id") != str(binding.snapshot_id)
    ):
        raise DomainError("AUDIO_BINDING_REQUIRED", "生产音频须绑定当前精确剧本台词", 409)
