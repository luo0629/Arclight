"""Validate bound dialogue audio before review, selection or locking."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audio_production import (
    approved_script,
    input_context,
    require_line,
    require_snapshot,
    write_episode,
)
from arclight.application.invalidation import input_expired
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.models import AssetVersion, DependencyManifest, StoredFile, VisualAsset


def require_audio_version(
    db: Session,
    settings: Settings,
    project_id: UUID,
    asset: VisualAsset,
    version: AssetVersion,
    actor: str,
) -> None:
    bindings = list(
        db.scalars(select(AudioLineBinding).where(AudioLineBinding.asset_version_id == version.id))
    )
    if (
        len(bindings) != 1
        or asset.asset_type != "AUDIO"
        or asset.project_id != project_id
        or version.asset_id != asset.id
    ):
        raise DomainError("AUDIO_BINDING_REQUIRED", "音频须有唯一的精确台词绑定才能审核或选用", 409)
    if version.stale:
        raise DomainError("AUDIO_VERSION_NOT_USABLE", "失效音频不能审核或选用", 409)
    binding = bindings[0]
    snapshot = require_snapshot(db, binding.snapshot_id, actor, write=True)
    require_line(snapshot, binding.line_id)
    episode = write_episode(db, snapshot.episode_id, actor)
    if episode.project_id != project_id:
        raise DomainError("AUDIO_BINDING_REQUIRED", "音频台词绑定不属于本项目", 409)
    approved_script(db, episode, snapshot.script_version_id, actor)
    manifest = db.get(DependencyManifest, binding.dependency_manifest_id)
    frozen = manifest.snapshot_json if manifest is not None else {}
    inputs = frozen.get("inputs")
    if (
        manifest is None
        or manifest.project_id != project_id
        or manifest.target_asset_id != asset.id
        or version.metadata_json.get("dependency_manifest_id") != str(manifest.id)
        or frozen.get("context") != input_context(snapshot, binding.line_id)
        or not isinstance(inputs, list)
        or len(inputs) != 1
        or not isinstance(inputs[0], dict)
        or inputs[0].get("source_asset_version_id") != str(snapshot.script_version_id)
        or inputs[0].get("checksum") != snapshot.script_sha256
        or inputs[0].get("dependency_type") != "CONTENT_DEPENDENCY"
        or inputs[0].get("selectors") != [f"dialogue:{binding.line_id}"]
    ):
        raise DomainError("AUDIO_BINDING_REQUIRED", "音频缺少本快照台词的精确输入来源", 409)
    if input_expired(db, project_id, inputs[0], "AUDIO"):
        raise DomainError("AUDIO_LINE_NOT_USABLE", "该台词已被批准修改，不能审核或选用旧音频", 409)
    files = list(db.scalars(select(StoredFile).where(StoredFile.asset_version_id == version.id)))
    if len(files) != 1:
        raise DomainError("AUDIO_FILE_INVALID", "音频须有唯一的不可变文件回执", 409)
    file = files[0]
    policy = POLICIES.get(file.media_type)
    if (
        file.id != binding.file_id
        or file.project_id != project_id
        or file.checksum != version.checksum
        or file.object_key != version.object_key
        or not file.media_type.startswith("audio/")
        or policy is None
        or not policy.purpose.intersection({"AUDIO", "MUSIC", "GENERATED_AUDIO"})
        or file.byte_size <= 0
        or not file.rights_confirmed
        or not file.source_note.strip()
        or not file.rights_note.strip()
    ):
        raise DomainError("AUDIO_FILE_INVALID", "音频文件及来源授权不符合精确绑定", 409)
    data = file_storage(settings, file).read(file.object_key, policy.max_bytes, file.checksum)
    if len(data) != file.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "音频大小与不可变文件回执不符", 409)
    details = probe(data, file.media_type)
    if details["duration_ms"] <= 0 or details["silence"]["silent"]:
        raise DomainError("AUDIO_SILENT", "音频没有有效语音，不能审核或选用", 422)
    if details["duration_ms"] != binding.duration_ms:
        raise DomainError("AUDIO_OUTPUT_MISMATCH", "音频实测时长与不可变台词绑定不符", 422)
