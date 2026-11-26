"""Resolve exact existing registry/file receipts without following mutable selection pointers."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.dependencies import exact_version, lock_dependency_graph
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES
from arclight.domain.render_manifests import RenderFileReference, RenderSource
from arclight.domain.timelines import TimelineSnapshot
from arclight.media.bundled_font import (
    BUNDLED_FONT_NAME,
    BUNDLED_FONT_RIGHTS,
    BUNDLED_FONT_SHA256,
    BUNDLED_FONT_SIZE,
    BUNDLED_FONT_SOURCE,
    BUNDLED_FONT_TYPE,
    bundled_subtitle_font,
    is_bundled_subtitle_font,
)
from arclight.media.validation import probe
from arclight.persistence.models import StoredFile, Storyboard
from arclight.storage.objects import ObjectStorage

SOURCE_TYPES = {
    "IMAGE": {"image/png", "image/jpeg"},
    "SHOT_IMAGE": {"image/png", "image/jpeg"},
    "VIDEO": {"video/mp4"},
    "AUDIO": {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac"},
    "MUSIC": {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac"},
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


def bundled_font_receipt(db: Session, settings: Settings, project_id: UUID) -> StoredFile:
    """Register one immutable receipt per project and published font digest."""
    lock_dependency_graph(db, project_id)
    file_id = uuid5(project_id, f"subtitle-font:{BUNDLED_FONT_SHA256}")
    key = f"validated/{project_id}/{file_id}"
    row = db.get(StoredFile, file_id)
    if row is not None:
        if (
            row.project_id != project_id
            or row.object_key != key
            or row.checksum != BUNDLED_FONT_SHA256
            or row.byte_size != BUNDLED_FONT_SIZE
            or row.media_type != BUNDLED_FONT_TYPE
            or row.source_note != BUNDLED_FONT_SOURCE
            or row.rights_note != BUNDLED_FONT_RIGHTS
            or not row.rights_confirmed
        ):
            raise DomainError("RENDER_FONT_INVALID", "内置字幕字体的登记记录不一致", 409)
        file_storage(settings, row).verify(
            key, BUNDLED_FONT_SIZE, BUNDLED_FONT_SHA256, BUNDLED_FONT_SIZE
        )
        return row

    try:
        data = bundled_subtitle_font()
    except (OSError, ValueError):
        raise DomainError("RENDER_FONT_UNAVAILABLE", "内置字幕字体文件不可用", 503) from None
    store = ObjectStorage(settings)
    try:
        store.put(key, data, BUNDLED_FONT_TYPE)
    except DomainError as exc:
        if exc.code != "OBJECT_EXISTS":
            raise
        # A rolled-back admission may already have written these immutable bytes.
        store.verify(key, BUNDLED_FONT_SIZE, BUNDLED_FONT_SHA256, BUNDLED_FONT_SIZE)
    row = StoredFile(
        id=file_id,
        project_id=project_id,
        asset_version_id=None,
        storage_backend=settings.object_storage_backend.upper(),
        bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
        object_key=key,
        checksum=BUNDLED_FONT_SHA256,
        byte_size=BUNDLED_FONT_SIZE,
        media_type=BUNDLED_FONT_TYPE,
        original_name=BUNDLED_FONT_NAME,
        source_note=BUNDLED_FONT_SOURCE,
        rights_note=BUNDLED_FONT_RIGHTS,
        rights_confirmed=True,
        created_by="service-owner",
    )
    db.add(row)
    db.flush()
    return row


def capture_font(
    db: Session, settings: Settings, project_id: UUID, file_id: UUID | None, *, required: bool
) -> RenderFileReference | None:
    if file_id is None and required:
        return file_reference(bundled_font_receipt(db, settings, project_id))
    reference = font_facts(db, project_id, file_id, required=required)
    if reference is None:
        return None
    row = db.get(StoredFile, reference.file_id)
    assert row is not None
    data = file_storage(settings, row).read(
        row.object_key, POLICIES[row.media_type].max_bytes, row.checksum
    )
    if len(data) != row.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "字体大小与不可变回执不符", 409)
    if not is_bundled_subtitle_font(data, row.media_type):
        probe(data, row.media_type)
    return reference


def font_facts(
    db: Session, project_id: UUID, file_id: UUID | None, *, required: bool
) -> RenderFileReference | None:
    """Database-only counterpart used inside FLOW-03 transactions."""
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
    return file_reference(row)


def capture_sources(
    db: Session, settings: Settings, project_id: UUID, timeline: TimelineSnapshot
) -> tuple[RenderSource, ...]:
    sources = source_facts(db, project_id, timeline)
    for source in sources:
        if source.asset_type == "MUSIC":
            version, asset = exact_version(db, project_id, source.asset_version_id)
            if asset.current_version_id != version.id:
                raise DomainError("ASSET_NOT_SELECTED", "音乐音效须显式选用本精确批准版本", 409)
        if source.file is not None:
            row = db.get(StoredFile, source.file.file_id)
            assert row is not None
            file_storage(settings, row).verify(
                row.object_key, row.byte_size, row.checksum, POLICIES[row.media_type].max_bytes
            )
    return sources


def source_facts(
    db: Session, project_id: UUID, timeline: TimelineSnapshot
) -> tuple[RenderSource, ...]:
    """Resolve exact registry and file receipts without object or parser I/O."""
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
        if asset.asset_type == "MUSIC":
            from arclight.application.music_assets import music_resource_facts

            music_resource_facts(db, project_id, asset, version)
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
