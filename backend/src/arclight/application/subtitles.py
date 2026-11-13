"""Produce subtitles from sealed timelines without altering existing selections."""

import hashlib
from datetime import UTC
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.asset_mutations import change_asset
from arclight.application.asset_objects import object_key, write_object
from arclight.application.audio_production import require_line, write_episode
from arclight.application.audio_versions import require_audio_version
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.dependencies import (
    attach_output,
    capture_manifest,
    exact_version,
    validate_manifest_inputs,
)
from arclight.application.files import record_asset_file
from arclight.application.invalidation import input_expired
from arclight.application.render_inputs import file_storage
from arclight.application.timeline_store import read_timeline
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.dependencies import DependencyInput, ManifestCreate
from arclight.domain.file_policy import POLICIES
from arclight.domain.subtitles import (
    EditedSubtitleDocument,
    SubtitleAssetMetadata,
    SubtitleCue,
    SubtitleDisplayCue,
    SubtitleDocument,
    SubtitleEdit,
    SubtitleGenerate,
    SubtitleTrackDocument,
    SubtitleTrackRead,
    srt_bytes,
    visible_cues,
)
from arclight.domain.timelines import ClipSnapshot
from arclight.domain.versions import digest
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    Episode,
    StoredFile,
    VisualAsset,
)
from arclight.persistence.subtitles import SubtitleProduction
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack


def invalid_history() -> DomainError:
    return DomainError("SUBTITLE_HISTORY_INVALID", "字幕不可变生产来源或文件回执不完整", 409)


def subtitle_document(db: Session, row: SubtitleProduction) -> SubtitleTrackDocument:
    try:
        document: SubtitleTrackDocument
        if row.snapshot_json.get("schema_version") == "subtitle-track/2":
            document = EditedSubtitleDocument.model_validate(row.snapshot_json)
        else:
            document = SubtitleDocument.model_validate(row.snapshot_json)
    except ValidationError:
        raise invalid_history() from None
    if document.episode_id != row.episode_id or document.timeline_id != row.timeline_id:
        raise invalid_history()
    return document


def production_record(db: Session, version: AssetVersion) -> SubtitleProduction | None:
    row = db.scalar(
        select(SubtitleProduction).where(SubtitleProduction.asset_version_id == version.id)
    )
    asset = db.get(VisualAsset, version.asset_id)
    marked = "subtitle_production_id" in version.metadata_json or (
        asset is not None and "subtitle_production" in asset.metadata_json
    )
    if row is None and marked:
        raise invalid_history()
    return row


def validate_provenance(
    db: Session, row: SubtitleProduction, document: SubtitleTrackDocument, version: AssetVersion
) -> None:
    """Verify frozen declarations using historical facts, never current selections."""
    timeline = db.get(Timeline, row.timeline_id)
    timeline_version = db.get(AssetVersion, row.timeline_id)
    manifest = db.get(DependencyManifest, row.dependency_manifest_id)
    if (
        timeline is None
        or not timeline.sealed
        or timeline.episode_id != row.episode_id
        or timeline_version is None
        or timeline_version.checksum != document.timeline_sha256
        or manifest is None
        or manifest.snapshot_json.get("context")
        != {"subtitle_document_sha256": digest(row.snapshot_json)}
    ):
        raise invalid_history()
    expected_inputs = {
        (str(item.source_asset_version_id), item.dependency_type): item.selectors
        for item in document_inputs(document)
    }
    actual_inputs = manifest.snapshot_json.get("inputs", [])
    if len(actual_inputs) != len(expected_inputs):
        raise invalid_history()
    for item in actual_inputs:
        key = (item.get("source_asset_version_id"), item.get("dependency_type"))
        source = db.get(AssetVersion, UUID(item["source_asset_version_id"]))
        if (
            key not in expected_inputs
            or item.get("selectors") != expected_inputs[key]
            or source is None
            or source.checksum != item.get("checksum")
        ):
            raise invalid_history()
    for cue in document.cues:
        source_clip = db.get(TimelineClip, cue.source_clip_id)
        track = db.get(TimelineTrack, source_clip.track_id) if source_clip else None
        binding = db.get(AudioLineBinding, cue.audio_binding_id)
        snapshot = db.get(AudioSnapshot, cue.audio_snapshot_id)
        audio = db.get(AssetVersion, cue.audio_asset_version_id)
        audio_file = db.get(StoredFile, cue.audio_file_id)
        if (
            source_clip is None
            or source_clip.timeline_id != row.timeline_id
            or track is None
            or track.kind != "VOICE"
            or track.timeline_id != row.timeline_id
            or source_clip.source_asset_version_id != cue.audio_asset_version_id
            or source_clip.source_sha256 != cue.audio_sha256
            or source_clip.start_ms != cue.start_ms
            or source_clip.duration_ms != cue.duration_ms
            or source_clip.source_start_ms != cue.source_start_ms
            or source_clip.script_version_id != cue.script_version_id
            or source_clip.line_id != cue.line_id
            or binding is None
            or binding.snapshot_id != cue.audio_snapshot_id
            or binding.asset_version_id != cue.audio_asset_version_id
            or binding.file_id != cue.audio_file_id
            or binding.line_id != cue.line_id
            or cue.source_start_ms + cue.duration_ms > binding.duration_ms
            or snapshot is None
            or snapshot.episode_id != row.episode_id
            or snapshot.script_version_id != cue.script_version_id
            or snapshot.script_sha256 != cue.script_sha256
            or audio is None
            or audio.checksum != cue.audio_sha256
            or audio_file is None
            or audio_file.asset_version_id != audio.id
            or audio_file.checksum != cue.audio_sha256
        ):
            raise invalid_history()
        line = require_line(snapshot, cue.line_id)
        if (line.text, line.speaker, line.line_type) != (cue.text, cue.speaker, cue.line_type):
            raise invalid_history()
    if isinstance(document, EditedSubtitleDocument):
        validate_edit_provenance(db, row, document, version, timeline)


def validate_edit_provenance(
    db: Session,
    row: SubtitleProduction,
    document: EditedSubtitleDocument,
    version: AssetVersion,
    timeline: Timeline,
) -> None:
    parent = db.get(AssetVersion, document.edit_source_version_id)
    parent_row = db.scalar(
        select(SubtitleProduction).where(
            SubtitleProduction.asset_version_id == document.edit_source_version_id
        )
    )
    if (
        parent is None
        or parent_row is None
        or parent.asset_id != version.asset_id
        or parent.version_no >= version.version_no
        or parent_row.episode_id != row.episode_id
        or parent_row.timeline_id != row.timeline_id
        or parent.checksum != document.edit_source_checksum
        or digest(parent_row.snapshot_json) != document.edit_source_document_sha256
        or parent.metadata_json.get("subtitle_document_sha256")
        != document.edit_source_document_sha256
    ):
        raise invalid_history()
    # The parent is an immutable earlier receipt, not a current selection or a recursive load.
    source = subtitle_document(db, parent_row)
    if (
        source.timeline_sha256 != document.timeline_sha256
        or source.cues != document.cues
        or any(cue.end_ms > timeline.duration_ms for cue in document.display_cues)
        or hashlib.sha256(srt_bytes(source)).hexdigest() != parent.checksum
    ):
        raise invalid_history()


def checked_production(
    db: Session, settings: Settings, row: SubtitleProduction
) -> tuple[SubtitleTrackDocument, AssetVersion, VisualAsset, StoredFile]:
    episode = db.get(Episode, row.episode_id)
    if episode is None:
        raise invalid_history()
    version, asset = exact_version(db, episode.project_id, row.asset_version_id)
    document = subtitle_document(db, row)
    validate_provenance(db, row, document, version)
    file = db.get(StoredFile, row.file_id)
    manifest = db.get(DependencyManifest, row.dependency_manifest_id)
    output = db.scalar(
        select(DependencyOutput).where(DependencyOutput.asset_version_id == version.id)
    )
    expected = srt_bytes(document)
    checksum = hashlib.sha256(expected).hexdigest()
    if (
        asset.asset_type != "SUBTITLE"
        or asset.metadata_json
        != SubtitleAssetMetadata(episode_id=episode.id, subtitle_production=1).model_dump(
            mode="json"
        )
        or version.metadata_json.get("subtitle_production_id") != str(row.id)
        or version.metadata_json.get("subtitle_document_sha256") != digest(row.snapshot_json)
        or version.metadata_json.get("dependency_manifest_id") != str(row.dependency_manifest_id)
        or version.checksum != checksum
        or manifest is None
        or manifest.project_id != episode.project_id
        or manifest.target_asset_id != asset.id
        or output is None
        or output.dependency_manifest_id != manifest.id
        or file is None
        or file.asset_version_id != version.id
        or file.project_id != episode.project_id
        or file.object_key != version.object_key
        or file.checksum != checksum
        or file.byte_size != len(expected)
        or file.media_type != "text/plain"
        or not file.original_name.endswith(".srt")
        or not file.rights_confirmed
        or not file.source_note.strip()
        or not file.rights_note.strip()
    ):
        raise invalid_history()
    actual = file_storage(settings, file).read(
        file.object_key, POLICIES["text/plain"].max_bytes, file.checksum
    )
    if actual != expected:
        raise invalid_history()
    return document, version, asset, file


def subtitle_clips(document: SubtitleTrackDocument, version: AssetVersion) -> list[ClipSnapshot]:
    return [
        ClipSnapshot(
            id=uuid5(version.id, f"subtitle-cue:{cue.cue_no}"),
            ordinal=cue.cue_no - 1,
            source_asset_version_id=version.id,
            source_sha256=version.checksum,
            start_ms=cue.start_ms,
            duration_ms=cue.duration_ms,
            source_start_ms=0,
            script_version_id=cue.script_version_id,
            line_id=cue.line_id,
            audio_version_id=cue.audio_asset_version_id,
        )
        for cue in visible_cues(document)
    ]


def read_subtitle(
    db: Session, settings: Settings, asset_version_id: UUID, actor: str
) -> SubtitleTrackRead:
    row = db.scalar(
        select(SubtitleProduction).where(SubtitleProduction.asset_version_id == asset_version_id)
    )
    if row is None:
        raise DomainError("NOT_FOUND", "字幕生产版本不存在", 404)
    require_episode(db, row.episode_id, actor)
    document, version, asset, file = checked_production(db, settings, row)
    return SubtitleTrackRead(
        id=version.id,
        asset_id=asset.id,
        version_no=version.version_no,
        status=version.status,
        stale=version.stale,
        checksum=version.checksum,
        dependency_manifest_id=row.dependency_manifest_id,
        file_id=file.id,
        timeline_id=row.timeline_id,
        created_at=row.created_at.replace(tzinfo=UTC)
        if row.created_at.tzinfo is None
        else row.created_at.astimezone(UTC),
        cues=visible_cues(document),
        clips=subtitle_clips(document, version),
        schema_version=document.schema_version,
        source_cues=document.cues if isinstance(document, EditedSubtitleDocument) else None,
        edit_source_version_id=document.edit_source_version_id
        if isinstance(document, EditedSubtitleDocument)
        else None,
    )


def subtitle_history(
    db: Session,
    settings: Settings,
    episode_id: UUID,
    actor: str,
    limit: int = 50,
    offset: int = 0,
) -> list[SubtitleTrackRead]:
    require_episode(db, episode_id, actor)
    rows = db.scalars(
        select(SubtitleProduction)
        .where(SubtitleProduction.episode_id == episode_id)
        .order_by(SubtitleProduction.created_at.desc(), SubtitleProduction.id)
        .limit(limit)
        .offset(offset)
    )
    return [read_subtitle(db, settings, row.asset_version_id, actor) for row in rows]


def source_cues(
    db: Session, settings: Settings, episode: Episode, timeline_id: UUID, actor: str
) -> SubtitleDocument:
    timeline = read_timeline(db, timeline_id, actor)
    if timeline.snapshot.episode_id != episode.id:
        raise DomainError("SUBTITLE_SOURCE_INVALID", "字幕来源时间轴必须属于本集", 422)
    clips = sorted(
        (
            clip
            for track in timeline.snapshot.tracks
            if track.kind == "VOICE"
            for clip in track.clips
        ),
        key=lambda clip: (clip.start_ms, str(clip.id)),
    )
    if not clips:
        raise DomainError("SUBTITLE_VOICE_REQUIRED", "时间轴必须有精确配音片段才能生成字幕", 422)
    validated: set[UUID] = set()
    cues: list[SubtitleCue] = []
    for clip in clips:
        version, asset = exact_version(db, episode.project_id, clip.source_asset_version_id)
        if version.id not in validated:
            require_audio_version(db, settings, episode.project_id, asset, version, actor)
            validated.add(version.id)
        binding = db.scalar(
            select(AudioLineBinding).where(AudioLineBinding.asset_version_id == version.id)
        )
        if binding is None:
            raise invalid_history()
        snapshot = db.get(AudioSnapshot, binding.snapshot_id)
        if snapshot is None:
            raise invalid_history()
        line = require_line(snapshot, binding.line_id)
        if (
            snapshot.episode_id != episode.id
            or clip.script_version_id != snapshot.script_version_id
            or clip.line_id != binding.line_id
            or clip.source_sha256 != version.checksum
            or clip.source_start_ms + clip.duration_ms > binding.duration_ms
        ):
            raise DomainError(
                "SUBTITLE_SOURCE_INVALID", "配音片段与精确台词或实测音频范围不符", 422
            )
        try:
            cue = SubtitleCue(
                cue_no=len(cues) + 1,
                text=line.text,
                speaker=line.speaker,
                line_type=line.line_type,
                start_ms=clip.start_ms,
                duration_ms=clip.duration_ms,
                source_start_ms=clip.source_start_ms,
                source_clip_id=clip.id,
                script_version_id=snapshot.script_version_id,
                script_sha256=snapshot.script_sha256,
                line_id=line.line_id,
                audio_asset_version_id=version.id,
                audio_sha256=version.checksum,
                audio_snapshot_id=snapshot.id,
                audio_binding_id=binding.id,
                audio_file_id=binding.file_id,
            )
        except ValidationError:
            raise DomainError(
                "SUBTITLE_TEXT_INVALID", "台词不符合规范字幕文本或时间范围", 422
            ) from None
        cues.append(cue)
    return SubtitleDocument(
        episode_id=episode.id,
        timeline_id=timeline.id,
        timeline_sha256=timeline.content_sha256,
        cues=cues,
    )


def document_inputs(document: SubtitleTrackDocument) -> list[DependencyInput]:
    audio_ids = sorted({cue.audio_asset_version_id for cue in document.cues}, key=str)
    scripts: dict[UUID, set[str]] = {}
    for cue in document.cues:
        scripts.setdefault(cue.script_version_id, set()).add(f"dialogue:{cue.line_id}")
    if any(len(selectors) > 128 for selectors in scripts.values()):
        raise DomainError("SUBTITLE_INPUT_LIMIT", "字幕精确来源超出当前输入清单范围", 422)
    inputs = [
        DependencyInput(
            source_asset_version_id=document.timeline_id, dependency_type="SEQUENCE_DEPENDENCY"
        ),
        *[
            DependencyInput(source_asset_version_id=value, dependency_type="CONTENT_DEPENDENCY")
            for value in audio_ids
        ],
        *[
            DependencyInput(
                source_asset_version_id=version_id,
                dependency_type="CONTENT_DEPENDENCY",
                selectors=sorted(selectors),
            )
            for version_id, selectors in sorted(scripts.items(), key=lambda item: str(item[0]))
        ],
    ]
    if len(inputs) > 64 or any(len(item.selectors) > 128 for item in inputs):
        raise DomainError("SUBTITLE_INPUT_LIMIT", "字幕精确来源超出当前输入清单范围", 422)
    return inputs


def store_subtitle_document(
    db: Session,
    settings: Settings,
    episode: Episode,
    asset: VisualAsset,
    expected_revision: int | None,
    creation_key: UUID,
    fingerprint: str,
    document: SubtitleTrackDocument,
    actor: str,
) -> SubtitleTrackRead:
    """Publish a new immutable document through the existing subtitle production path."""
    inputs = document_inputs(document)
    data = srt_bytes(document)
    text_limit = POLICIES["text/plain"].max_bytes
    if text_limit is not None and len(data) > text_limit:
        raise DomainError("SUBTITLE_TEXT_INVALID", "字幕文本超出文本文件策略范围", 422)
    if asset.status in {"LOCKED", "ARCHIVED", "STALE", "REJECTED"}:
        raise DomainError("ASSET_READ_ONLY", "锁定、归档或失效字幕资产不能生成新版本", 409)
    change_asset(db, asset, expected_revision if expected_revision is not None else 0)
    manifest, _ = capture_manifest(
        db,
        episode.project_id,
        actor,
        ManifestCreate(
            creation_key=uuid5(creation_key, "subtitle-manifest"),
            target_asset_id=asset.id,
            inputs=inputs,
            context={"subtitle_document_sha256": digest(document.model_dump(mode="json"))},
        ),
    )
    if any(
        input_expired(db, episode.project_id, item, "SUBTITLE")
        for item in manifest.snapshot_json["inputs"]
    ):
        raise DomainError("SUBTITLE_SOURCE_NOT_USABLE", "字幕来源已失效，请使用新的精确版本", 409)
    version_id, production_id = uuid4(), uuid4()
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
        object_key=object_key(episode.project_id, version_id, settings),
        checksum=hashlib.sha256(data).hexdigest(),
        creation_key=creation_key,
        created_by=actor,
        status="DRAFT",
        metadata_json={
            "subtitle_production_id": str(production_id),
            "subtitle_document_sha256": digest(document.model_dump(mode="json")),
            "dependency_manifest_id": str(manifest.id),
            "storage_backend": settings.object_storage_backend.upper(),
            "media": {
                "media_type": "text/plain",
                "byte_size": len(data),
                "original_name": f"subtitles-{version_id}.srt",
                "source_note": f"精确时间轴 {document.timeline_id} 的已批准台词和配音",
                "rights_note": "继承本项目已确认授权的剧本和配音来源",
                "rights_confirmed": True,
            },
        },
    )
    write_object(db, settings, episode.project_id, version.id, data, "text/plain")
    db.add(version)
    db.flush()
    record_asset_file(db, settings, episode.project_id, version)
    file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if file is None:
        raise invalid_history()
    attach_output(db, episode.project_id, manifest.id, version)
    row = SubtitleProduction(
        id=production_id,
        episode_id=episode.id,
        timeline_id=document.timeline_id,
        asset_version_id=version.id,
        dependency_manifest_id=manifest.id,
        file_id=file.id,
        creation_key=creation_key,
        request_fingerprint=fingerprint,
        snapshot_json=document.model_dump(mode="json"),
        created_by=actor,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        episode.project_id,
        actor,
        "subtitle.edited" if isinstance(document, EditedSubtitleDocument) else "subtitle.generated",
        version.id,
        {
            "timeline_id": str(document.timeline_id),
            "cue_count": len(document.cues),
            "dependency_manifest_id": str(manifest.id),
            "file_id": str(file.id),
            **(
                {"edit_source_version_id": str(document.edit_source_version_id)}
                if isinstance(document, EditedSubtitleDocument)
                else {}
            ),
        },
    )
    return read_subtitle(db, settings, version.id, actor)


def generate_subtitles(
    db: Session, settings: Settings, episode_id: UUID, payload: SubtitleGenerate, actor: str
) -> SubtitleTrackRead:
    episode = write_episode(db, episode_id, actor)
    fingerprint = digest(
        {
            "schema_version": "subtitle-request/1",
            "episode_id": str(episode_id),
            "actor": actor,
            **payload.model_dump(mode="json", exclude={"creation_key", "expected_revision"}),
        }
    )
    previous = db.scalar(
        select(SubtitleProduction).where(
            SubtitleProduction.episode_id == episode_id,
            SubtitleProduction.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一生成标识不能更换字幕来源或目标", 409)
        return read_subtitle(db, settings, previous.asset_version_id, actor)
    document = source_cues(db, settings, episode, payload.timeline_id, actor)
    if payload.target_asset_id is None:
        asset = VisualAsset(
            id=uuid4(),
            project_id=episode.project_id,
            asset_type="SUBTITLE",
            name=f"字幕 · {episode.id}",
            metadata_json=SubtitleAssetMetadata(
                episode_id=episode.id, subtitle_production=1
            ).model_dump(mode="json"),
            created_by=actor,
            status="DRAFT",
        )
        db.add(asset)
        db.flush()
    else:
        existing_asset = db.get(VisualAsset, payload.target_asset_id, populate_existing=True)
        if existing_asset is None or existing_asset.project_id != episode.project_id:
            raise DomainError("NOT_FOUND", "字幕目标资产不存在", 404)
        asset = existing_asset
        has_production = db.scalar(
            select(SubtitleProduction.id)
            .join(AssetVersion, AssetVersion.id == SubtitleProduction.asset_version_id)
            .where(AssetVersion.asset_id == asset.id, SubtitleProduction.episode_id == episode.id)
            .limit(1)
        )
        if has_production is None:
            raise DomainError("SUBTITLE_TARGET_INVALID", "目标必须是本集已有生产字幕资产", 422)
        if asset.asset_type != "SUBTITLE" or asset.metadata_json != SubtitleAssetMetadata(
            episode_id=episode.id, subtitle_production=1
        ).model_dump(mode="json"):
            raise DomainError("SUBTITLE_TARGET_INVALID", "目标必须是本集已有生产字幕资产", 422)
    return store_subtitle_document(
        db,
        settings,
        episode,
        asset,
        payload.expected_revision,
        payload.creation_key,
        fingerprint,
        document,
        actor,
    )


def edit_subtitles(
    db: Session,
    settings: Settings,
    asset_version_id: UUID,
    payload: SubtitleEdit,
    actor: str,
) -> SubtitleTrackRead:
    source_row = db.scalar(
        select(SubtitleProduction).where(SubtitleProduction.asset_version_id == asset_version_id)
    )
    if source_row is None:
        raise DomainError("NOT_FOUND", "字幕生产版本不存在", 404)
    episode = write_episode(db, source_row.episode_id, actor)
    fingerprint = digest(
        {
            "schema_version": "subtitle-edit-request/1",
            "episode_id": str(episode.id),
            "actor": actor,
            "source_asset_version_id": str(asset_version_id),
            "source_document_sha256": digest(source_row.snapshot_json),
            **payload.model_dump(mode="json", exclude={"creation_key", "expected_revision"}),
        }
    )
    previous = db.scalar(
        select(SubtitleProduction).where(
            SubtitleProduction.episode_id == episode.id,
            SubtitleProduction.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一编辑标识不能更换字幕来源或修改内容", 409)
        return read_subtitle(db, settings, previous.asset_version_id, actor)
    source, version, asset, _ = checked_production(db, settings, source_row)
    if version.status in {"LOCKED", "ARCHIVED"}:
        raise DomainError("ASSET_READ_ONLY", "锁定或归档字幕不能编辑，请先显式解除", 409)
    require_subtitle_version(db, settings, episode.project_id, asset, version, actor)
    displays = [
        SubtitleDisplayCue(
            cue_no=cue.cue_no,
            text=cue.text,
            start_ms=cue.start_ms,
            end_ms=cue.start_ms + cue.duration_ms,
        )
        for cue in visible_cues(source)
    ]
    for change in payload.changes:
        if change.cue_no > len(displays):
            raise DomainError("SUBTITLE_CUE_NOT_FOUND", "修改的字幕句不存在", 422)
        index = change.cue_no - 1
        values = displays[index].model_dump()
        if change.text is not None:
            values["text"] = change.text
        if change.sync_to_audio:
            cue = source.cues[index]
            values["start_ms"] = cue.start_ms
            values["end_ms"] = cue.start_ms + cue.duration_ms
        elif change.start_ms is not None and change.end_ms is not None:
            values["start_ms"] = change.start_ms
            values["end_ms"] = change.end_ms
        displays[index] = SubtitleDisplayCue.model_validate(values)
    timeline = db.get(Timeline, source.timeline_id)
    if timeline is None or not timeline.sealed:
        raise invalid_history()
    if any(cue.end_ms > timeline.duration_ms for cue in displays):
        raise DomainError("SUBTITLE_TIME_INVALID", "字幕结束时间超出实际时间轴", 422)
    try:
        document = EditedSubtitleDocument(
            episode_id=episode.id,
            timeline_id=source.timeline_id,
            timeline_sha256=source.timeline_sha256,
            cues=source.cues,
            display_cues=displays,
            edit_source_version_id=version.id,
            edit_source_checksum=version.checksum,
            edit_source_document_sha256=digest(source_row.snapshot_json),
        )
    except ValidationError:
        raise DomainError("SUBTITLE_TIME_INVALID", "字幕句顺序或重叠范围不合法", 422) from None
    return store_subtitle_document(
        db,
        settings,
        episode,
        asset,
        payload.expected_revision,
        payload.creation_key,
        fingerprint,
        document,
        actor,
    )


def require_subtitle_version(
    db: Session,
    settings: Settings,
    project_id: UUID,
    asset: VisualAsset,
    version: AssetVersion,
    actor: str,
) -> None:
    row = production_record(db, version)
    if row is None:
        raise DomainError(
            "SUBTITLE_PROVENANCE_REQUIRED", "字幕须有精确生产来源才能审核、选用或锁定", 409
        )
    episode = require_episode(db, row.episode_id, actor, write=True)
    if episode.project_id != project_id or version.asset_id != asset.id or version.stale:
        raise DomainError("SUBTITLE_VERSION_NOT_USABLE", "字幕版本不属于本项目或已失效", 409)
    document, _, _, _ = checked_production(db, settings, row)
    manifest = db.get(DependencyManifest, row.dependency_manifest_id)
    if manifest is None:
        raise invalid_history()
    validate_manifest_inputs(db, project_id, manifest, "SUBTITLE")
    if any(
        input_expired(db, project_id, item, "SUBTITLE") for item in manifest.snapshot_json["inputs"]
    ):
        raise DomainError("SUBTITLE_SOURCE_NOT_USABLE", "字幕精确来源已失效", 409)
    for audio_id in {cue.audio_asset_version_id for cue in document.cues}:
        audio_version, audio_asset = exact_version(db, project_id, audio_id)
        require_audio_version(db, settings, project_id, audio_asset, audio_version, actor)


def validate_subtitle_clip(db: Session, episode: Episode, clip: ClipSnapshot) -> None:
    version, asset = exact_version(db, episode.project_id, clip.source_asset_version_id)
    row = production_record(db, version)
    if row is None:
        return
    document = subtitle_document(db, row)
    validate_provenance(db, row, document, version)
    if (
        row.episode_id != episode.id
        or asset.asset_type != "SUBTITLE"
        or version.metadata_json.get("subtitle_production_id") != str(row.id)
        or version.metadata_json.get("subtitle_document_sha256") != digest(row.snapshot_json)
        or clip.source_start_ms != 0
        or not any(
            cue.start_ms == clip.start_ms
            and cue.duration_ms == clip.duration_ms
            and cue.script_version_id == clip.script_version_id
            and cue.line_id == clip.line_id
            and cue.audio_asset_version_id == clip.audio_version_id
            for cue in visible_cues(document)
        )
    ):
        raise DomainError("SUBTITLE_CLIP_MISMATCH", "字幕片段与不可变生产台词、音频或时点不符", 422)
