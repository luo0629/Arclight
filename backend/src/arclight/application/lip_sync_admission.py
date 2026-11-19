"""Exact dialogue and mouth-correction receipts shared by review and formal assembly."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import PerformanceDialogue, ShotContentV3, read_shot_content
from arclight.application.audio_production import (
    audio_line_manifest,
    manifest_matches_line,
    require_live_line_inputs,
    validate_audio_line,
)
from arclight.application.dependencies import exact_version
from arclight.core.errors import DomainError
from arclight.domain.lip_sync import MAX_SYNC_MS, LipSyncReceipt
from arclight.media.audio import AUDIO_FORMATS
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import AssetVersion, Shot, StoredFile, Storyboard


def shot_dialogue(shot: Shot) -> list[PerformanceDialogue]:
    try:
        content = read_shot_content(shot.content_json)
    except ValidationError as error:
        raise DomainError("INVALID_STORYBOARD", "镜头的口型适用性合同无效", 409) from error
    return content.performance.dialogue if isinstance(content, ShotContentV3) else []


def required_lines(shot: Shot) -> list[str]:
    return [line.line_id for line in shot_dialogue(shot) if line.lipsync == "required"]


def exact_sync_audio(
    db: Session, project_id: UUID, board: Storyboard, line_id: str, version_id: UUID
) -> tuple[AudioLineBinding, StoredFile]:
    version, asset = exact_version(db, project_id, version_id)
    binding = db.scalar(
        select(AudioLineBinding).where(AudioLineBinding.asset_version_id == version.id)
    )
    snapshot = db.get(AudioSnapshot, binding.snapshot_id) if binding else None
    manifest = audio_line_manifest(db, version)
    if (
        version.stale
        or version.status not in {"APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
        or asset.asset_type != "AUDIO"
        or asset.current_version_id != version.id
        or binding is None
        or binding.line_id != line_id
        or not 0 < binding.duration_ms <= MAX_SYNC_MS
        or snapshot is None
        or snapshot.episode_id != board.episode_id
        or manifest is None
        or manifest.id != binding.dependency_manifest_id
        or manifest.project_id != project_id
        or manifest.target_asset_id != asset.id
        or not manifest_matches_line(manifest, snapshot, line_id)
    ):
        raise DomainError("LIP_SYNC_AUDIO_INVALID", "口型需要本分镜台词当前选用的精确音频", 409)
    validate_audio_line(db, version.id, board.script_version_id, line_id)
    require_live_line_inputs(db, project_id, manifest)
    file = db.get(StoredFile, binding.file_id, populate_existing=True)
    if (
        file is None
        or file.project_id != project_id
        or file.asset_version_id != version.id
        or file.object_key != version.object_key
        or file.checksum != version.checksum
        or file.media_type not in AUDIO_FORMATS
        or not file.rights_confirmed
        or not file.source_note.strip()
        or not file.rights_note.strip()
    ):
        raise DomainError("LIP_SYNC_AUDIO_INVALID", "台词音频缺少精确、授权的私有文件回执", 409)
    return binding, file


def lip_sync_receipt(version: AssetVersion) -> LipSyncReceipt | None:
    value = version.metadata_json.get("lip_sync")
    if value is None:
        return None
    try:
        receipt = LipSyncReceipt.model_validate(value)
        if receipt.output_sha256 != version.checksum:
            raise ValueError("Mouth-pose output identity mismatch")
        return receipt
    except (ValidationError, ValueError) as error:
        raise DomainError(
            "LIP_SYNC_RECEIPT_INVALID", "口型校正回执与精确输出不一致", 409
        ) from error


def require_lip_sync(
    db: Session, project_id: UUID, shot: Shot, version: AssetVersion
) -> LipSyncReceipt | None:
    required = required_lines(shot)
    receipt = lip_sync_receipt(version)
    if not required and receipt is None:
        return None
    if receipt is None:
        raise DomainError("LIP_SYNC_REQUIRED", "可见对白必须完成口型校正及精确版本重审", 409)
    board = db.get(Storyboard, shot.storyboard_id)
    if (
        board is None
        or receipt.script_version_id != board.script_version_id
        or [line.line_id for line in receipt.lines] != required
    ):
        raise DomainError("LIP_SYNC_RECEIPT_INVALID", "口型回执未覆盖本版镜头的必需对白", 409)
    for line in receipt.lines:
        binding, file = exact_sync_audio(db, project_id, board, line.line_id, line.audio_version_id)
        if (file.id, file.checksum, binding.duration_ms) != (
            line.audio_file_id,
            line.audio_sha256,
            line.duration_ms,
        ):
            raise DomainError("LIP_SYNC_AUDIO_INVALID", "口型的精确台词音频已不匹配", 409)
    return receipt
