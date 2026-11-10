"""Resolve exact existing registry/file receipts without following mutable selection pointers."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.dependencies import exact_version
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES
from arclight.domain.render_manifests import RenderFileReference, RenderSource
from arclight.domain.timelines import TimelineSnapshot
from arclight.media.validation import probe
from arclight.persistence.models import StoredFile, Storyboard
from arclight.storage.objects import ObjectStorage

SOURCE_TYPES = {
    "IMAGE": {"image/png", "image/jpeg"},
    "SHOT_IMAGE": {"image/png", "image/jpeg"},
    "AUDIO": {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac"},
    "SUBTITLE": {"text/plain"},
}


def file_storage(settings: Settings, row: StoredFile) -> ObjectStorage:
    return ObjectStorage(
        settings.model_copy(
            update={
                "object_storage_backend": row.storage_backend.lower(),
                "s3_bucket": row.bucket or settings.s3_bucket,
            }
        )
    )


def file_reference(row: StoredFile) -> RenderFileReference:
    if not row.source_note.strip() or not row.rights_note.strip():
        raise DomainError("RENDER_RIGHTS_REQUIRED", "渲染文件必须保留来源及使用权声明", 409)
    return RenderFileReference(
        file_id=row.id,
        sha256=row.checksum,
        byte_size=row.byte_size,
        media_type=row.media_type,
        source_note=row.source_note,
        rights_note=row.rights_note,
        rights_confirmed=row.rights_confirmed,
    )


def capture_font(
    db: Session, settings: Settings, project_id: UUID, file_id: UUID | None, *, required: bool
) -> RenderFileReference | None:
    if file_id is None:
        if required:
            raise DomainError("RENDER_FONT_REQUIRED", "字幕必须指定精确且已授权的字体文件", 409)
        return None
    row = db.get(StoredFile, file_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "本项目的字体文件不存在", 404)
    if row.media_type not in {"font/ttf", "font/otf"}:
        raise DomainError("RENDER_FONT_INVALID", "只能使用已登记的TTF或OTF字体", 409)
    if not row.rights_confirmed:
        raise DomainError("RENDER_RIGHTS_REQUIRED", "字幕字体必须有明确使用授权", 409)
    reference = file_reference(row)
    data = file_storage(settings, row).read(
        row.object_key, POLICIES[row.media_type].max_bytes, row.checksum
    )
    if len(data) != row.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "字体大小与不可变回执不符", 409)
    probe(data, row.media_type)
    return reference


def capture_sources(
    db: Session, settings: Settings, project_id: UUID, timeline: TimelineSnapshot
) -> tuple[RenderSource, ...]:
    expected = {
        clip.source_asset_version_id: clip.source_sha256
        for track in timeline.tracks
        for clip in track.clips
    }
    identities = set(expected)
    identities.add(timeline.storyboard_id)
    board = db.get(Storyboard, timeline.storyboard_id)
    if board is None or board.episode_id != timeline.episode_id:
        raise DomainError("RENDER_INPUT_INVALID", "时间轴分镜范围无效", 409)
    identities.add(board.script_version_id)
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.script_version_id:
                identities.add(clip.script_version_id)
            if clip.audio_version_id:
                identities.add(clip.audio_version_id)
    sources = []
    for identity in sorted(identities):
        version, asset = exact_version(db, project_id, identity)
        if identity in expected and expected[identity] != version.checksum:
            raise DomainError("RENDER_INPUT_INVALID", "时间轴素材摘要与精确版本不符", 409)
        reference = None
        if asset.asset_type in SOURCE_TYPES:
            row = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == identity))
            if (
                row is None
                or row.project_id != project_id
                or row.checksum != version.checksum
                or row.object_key != version.object_key
                or row.media_type not in SOURCE_TYPES[asset.asset_type]
            ):
                raise DomainError("RENDER_INPUT_INVALID", "素材缺少一致的精确文件回执", 409)
            reference = file_reference(row)
            file_storage(settings, row).verify(
                row.object_key, row.byte_size, row.checksum, POLICIES[row.media_type].max_bytes
            )
        elif asset.asset_type not in {"SCRIPT", "STORYBOARD"}:
            raise DomainError("RENDER_INPUT_INVALID", "渲染来源类型尚不受支持", 409)
        sources.append(
            RenderSource(
                asset_id=asset.id,
                asset_version_id=version.id,
                asset_type=asset.asset_type,
                sha256=version.checksum,
                file=reference,
            )
        )
    return tuple(sources)
