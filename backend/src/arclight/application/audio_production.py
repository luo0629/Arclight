"""Prepare immutable dialogue inputs and register their original audio outputs."""

import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import SceneV3, ScriptContent, read_script_content
from arclight.application.audio_voice_profiles import (
    character_for,
    manifest_voice,
    performance_for,
    voice_selector,
)
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.content_dependencies import register_content
from arclight.application.content_references import validate_script_assets
from arclight.application.dependencies import capture_manifest, exact_version, lock_dependency_graph
from arclight.application.invalidation import input_expired
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.audio_production import (
    AudioInputCreate,
    AudioLine,
    AudioSnapshotCreate,
)
from arclight.domain.audio_voice_profiles import VoiceSelection
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.domain.file_policy import POLICIES
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.audio_tts import AudioTTSRequest
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
    validate_script_assets(db, episode.project_id, read_script_content(script.content_json))
    return script


def script_audio_lines(content: ScriptContent) -> list[AudioLine]:
    return [
        AudioLine(
            **line.model_dump(),
            scene_id=scene.scene_id,
            scene_no=scene.scene_no,
            sequence_no=index + 1,
        )
        for scene in content.scenes
        for index, line in enumerate(scene.dialogue)
    ]


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
    lines = script_audio_lines(read_script_content(script.content_json))
    registry = db.get(AssetVersion, script.id)
    assert registry is not None
    row = AudioSnapshot(
        episode_id=episode.id,
        script_version_id=script.id,
        script_sha256=registry.checksum,
        creation_key=payload.creation_key,
        lines_json=[line.model_dump(mode="json") for line in lines],
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


def line_inputs(
    row: AudioSnapshot, line_id: str, selection: VoiceSelection | None = None
) -> list[DependencyInput]:
    inputs = [
        DependencyInput(
            source_asset_version_id=row.script_version_id,
            dependency_type="CONTENT_DEPENDENCY",
            selectors=[f"dialogue:{line_id}"],
        )
    ]
    if selection is not None:
        inputs.append(
            DependencyInput(
                source_asset_version_id=selection.profile_version_id,
                dependency_type="CONTENT_DEPENDENCY",
                selectors=[voice_selector(row, line_id)],
            )
        )
    return inputs


def manifest_matches_line(manifest: DependencyManifest, row: AudioSnapshot, line_id: str) -> bool:
    frozen = manifest.snapshot_json.get("context")
    inputs = manifest.snapshot_json.get("inputs")
    if not isinstance(frozen, dict) or not isinstance(inputs, list):
        return False
    context = dict(frozen)
    selection = manifest_voice(context)
    context.pop("voice_selection", None)
    expected = line_inputs(row, line_id, selection)
    return (
        context == input_context(row, line_id)
        and (selection is None or selection.episode_id == row.episode_id)
        and len(inputs) == len(expected)
        and all(
            isinstance(actual, dict)
            and all(
                actual.get(key) == value for key, value in source.model_dump(mode="json").items()
            )
            for actual, source in zip(inputs, expected, strict=True)
        )
        and inputs[0].get("checksum") == row.script_sha256
    )


def require_live_line_inputs(db: Session, project_id: UUID, manifest: DependencyManifest) -> None:
    script, *profile = manifest.snapshot_json["inputs"]
    if input_expired(db, project_id, script, "AUDIO"):
        raise DomainError("AUDIO_LINE_NOT_USABLE", "该台词已被批准修改，请选择新的精确来源", 409)
    if any(input_expired(db, project_id, item, "AUDIO") for item in profile):
        raise DomainError("AUDIO_VOICE_PROFILE_CHANGED", "该台词的声音档案已变化，请重新核对", 409)


def line_target(
    db: Session,
    snapshot: AudioSnapshot,
    line_id: str,
    target_id: UUID | None,
    actor: str,
    *,
    profile_version_id: UUID | None = None,
) -> VisualAsset:
    """Allocate the same dialogue target for imports and generated recordings."""
    line = require_line(snapshot, line_id)
    episode = require_episode(db, snapshot.episode_id, actor, write=True)
    key = f"audio-line:{line_id}"
    if profile_version_id is not None:
        key = f"{key}:voice-profile:{profile_version_id}"
    identity = target_id or uuid5(snapshot.id, key)
    asset = db.get(VisualAsset, identity, populate_existing=True)
    if asset is None:
        if target_id is not None:
            raise DomainError("NOT_FOUND", "音频目标不存在", 404)
        asset = VisualAsset(
            id=identity,
            project_id=episode.project_id,
            asset_type="AUDIO",
            name=f"{line.speaker}：{line.text}"[:160],
            created_by=actor,
            metadata_json={"audio_snapshot_id": str(snapshot.id), "line_id": line_id},
        )
        db.add(asset)
        db.flush()
    if asset.project_id != episode.project_id:
        raise DomainError("NOT_FOUND", "音频目标不存在", 404)
    if asset.asset_type != "AUDIO":
        raise DomainError("AUDIO_TARGET_INVALID", "台词生产目标必须是音频资产", 422)
    return asset


def capture_line_inputs(
    db: Session,
    snapshot_id: UUID,
    line_id: str,
    actor: str,
    payload: AudioInputCreate,
    *,
    selection: VoiceSelection | None = None,
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
    context = input_context(row, line_id)
    if selection is not None:
        context["voice_selection"] = selection.model_dump(mode="json")
    manifest, created = capture_manifest(
        db,
        episode.project_id,
        actor,
        ManifestCreate(
            creation_key=payload.creation_key,
            target_asset_id=asset.id,
            inputs=line_inputs(row, line_id, selection),
            context=context,
        ),
    )
    if created:
        require_live_line_inputs(db, episode.project_id, manifest)
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
        or not manifest_matches_line(manifest, row, line_id)
    ):
        raise DomainError(
            "AUDIO_PROVENANCE_REQUIRED", "音频须在创建时固定本快照台词的输入清单", 409
        )
    selection = manifest_voice(manifest.snapshot_json["context"])
    if selection is not None:
        request = db.scalar(
            select(AudioTTSRequest).where(
                AudioTTSRequest.project_id == episode.project_id,
                AudioTTSRequest.dependency_manifest_id == manifest.id,
                AudioTTSRequest.snapshot_id == row.id,
                AudioTTSRequest.line_id == line_id,
                AudioTTSRequest.voice_version_id == selection.voice_version_id,
                AudioTTSRequest.target_asset_id == asset.id,
            )
        )
        if request is None or version.creation_key != uuid5(request.job_id, "audio-tts-result"):
            raise DomainError(
                "AUDIO_PROVENANCE_REQUIRED", "声音档案须来自本句的精确配音任务回执", 409
            )
    require_live_line_inputs(db, episode.project_id, manifest)
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


def audio_line_manifest(db: Session, version: AssetVersion) -> DependencyManifest | None:
    raw = version.metadata_json.get("dependency_manifest_id")
    try:
        manifest_id = UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return db.get(DependencyManifest, manifest_id) if manifest_id else None


def reusable_audio_line(
    db: Session, version: AssetVersion, snapshot: AudioSnapshot, script_id: UUID, line_id: str
) -> bool:
    """Compare the entire recorded performance while keeping the original binding."""
    manifest = audio_line_manifest(db, version)
    if manifest is None or not manifest_matches_line(manifest, snapshot, line_id):
        return False
    selection = manifest_voice(manifest.snapshot_json["context"])
    script = db.get(Script, script_id)
    if selection is None or script is None or script.episode_id != snapshot.episode_id:
        return False
    content = read_script_content(script.content_json)
    original = require_line(snapshot, line_id)
    if character_for(content, original.speaker) != selection.character_asset_id:
        return False
    for scene in content.scenes:
        if scene.scene_id != original.scene_id:
            continue
        if isinstance(scene, SceneV3) and scene.dramatic_beat.story_time != selection.story_time:
            return False
        for index, line in enumerate(scene.dialogue, start=1):
            if line.line_id == line_id:
                candidate = AudioLine(
                    **line.model_dump(),
                    scene_id=scene.scene_id,
                    scene_no=scene.scene_no,
                    sequence_no=index,
                )
                return (
                    candidate == original
                    and performance_for(db, content, scene, candidate) == selection.performance
                )
    return False


def validate_audio_line(db: Session, identity: UUID, script_id: UUID, line_id: str) -> None:
    """Enforce precise line provenance, allowing unchanged profiled recordings to be reused."""
    version = db.get(AssetVersion, identity)
    if version is None:
        return
    manifest = audio_line_manifest(db, version)
    context = manifest.snapshot_json.get("context", {}) if manifest else {}
    if "audio_snapshot_id" not in context:
        return
    binding = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == identity)
    )
    if (
        binding is None
        or binding.line_id != line_id
        or context.get("audio_snapshot_id") != str(binding.snapshot_id)
    ):
        raise DomainError("AUDIO_BINDING_REQUIRED", "生产音频须绑定当前精确剧本台词", 409)
    if context.get("script_version_id") != str(script_id):
        snapshot = db.get(AudioSnapshot, binding.snapshot_id)
        if (
            snapshot is None
            or version.stale
            or not reusable_audio_line(db, version, snapshot, script_id, line_id)
        ):
            raise DomainError("AUDIO_BINDING_REQUIRED", "生产音频须绑定当前精确剧本台词", 409)
