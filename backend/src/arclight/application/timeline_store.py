"""Read and append frozen timeline aggregates using the existing asset/dependency registry."""

from copy import deepcopy
from datetime import UTC
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.dependencies import attach_output, capture_manifest, exact_version
from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate
from arclight.domain.timelines import ClipSnapshot, TimelineRead, TimelineSnapshot, TrackSnapshot
from arclight.domain.versions import digest
from arclight.persistence.models import AssetVersion, Episode, VisualAsset
from arclight.persistence.timelines import (
    Timeline,
    TimelineClip,
    TimelineClipVisual,
    TimelineRevision,
    TimelineTrack,
)


def read_timeline(db: Session, timeline_id: UUID, actor: str) -> TimelineRead:
    row = db.get(Timeline, timeline_id)
    if row is None or not row.sealed:
        raise DomainError("NOT_FOUND", "时间轴版本不存在", 404)
    episode = require_episode(db, row.episode_id, actor)
    version, asset = exact_version(db, episode.project_id, row.id)
    tracks = list(
        db.scalars(
            select(TimelineTrack)
            .where(TimelineTrack.timeline_id == row.id)
            .order_by(TimelineTrack.ordinal)
        )
    )
    clips = list(
        db.scalars(
            select(TimelineClip)
            .where(TimelineClip.timeline_id == row.id)
            .order_by(TimelineClip.ordinal)
        )
    )
    visuals = {
        item.id: item.settings_json
        for item in db.scalars(
            select(TimelineClipVisual).where(TimelineClipVisual.timeline_id == row.id)
        )
    }
    snapshot = TimelineSnapshot(
        schema_version=version.metadata_json.get("timeline_schema_version", 1),
        episode_id=row.episode_id,
        storyboard_id=row.storyboard_id,
        default_duration_ms=row.default_duration_ms,
        duration_ms=row.duration_ms,
        output_profile=deepcopy(row.output_profile_json),
        tracks=[
            TrackSnapshot(
                id=track.id,
                kind=track.kind,
                ordinal=track.ordinal,
                clips=[
                    ClipSnapshot.model_validate(
                        {
                            **{
                                key: getattr(clip, key)
                                for key in ClipSnapshot.model_fields
                                if key != "visual"
                            },
                            "visual": visuals.get(clip.id),
                        }
                    )
                    for clip in clips
                    if clip.track_id == track.id
                ],
            )
            for track in tracks
        ],
    )
    if digest(snapshot.model_dump(mode="json")) != version.checksum:
        raise DomainError("TIMELINE_HISTORY_INVALID", "时间轴内容与精确版本摘要不匹配", 409)
    receipt = db.get(TimelineRevision, row.id)
    return TimelineRead(
        id=row.id,
        asset_id=asset.id,
        version_no=row.version_no,
        status=version.status,
        stale=version.stale,
        content_sha256=version.checksum,
        dependency_manifest_id=UUID(version.metadata_json["dependency_manifest_id"]),
        based_on_timeline_id=receipt.based_on_timeline_id if receipt else None,
        created_at=row.created_at.replace(tzinfo=UTC)
        if row.created_at.tzinfo is None
        else row.created_at.astimezone(UTC),
        snapshot=snapshot,
    )


def replay_timeline(
    db: Session, episode_id: UUID, key: UUID, request_hash: str, actor: str
) -> TimelineRead | None:
    previous = db.scalar(
        select(Timeline).where(Timeline.episode_id == episode_id, Timeline.creation_key == key)
    )
    if previous is None:
        return None
    if previous.request_sha256 != request_hash:
        raise DomainError("IDEMPOTENCY_CONFLICT", "同一保存键不能用于不同时间轴输入或基准", 409)
    return read_timeline(db, previous.id, actor)


def append_timeline(
    db: Session,
    episode: Episode,
    actor: str,
    key: UUID,
    request_hash: str,
    snapshot: TimelineSnapshot,
    *,
    base: Timeline | None = None,
) -> TimelineRead:
    """Append under the project write gate; capture_manifest also owns the shared DAG lock."""
    asset_id = uuid5(episode.id, "dependency:TIMELINE")
    asset = db.get(VisualAsset, asset_id)
    if asset is None:
        asset = VisualAsset(
            id=asset_id,
            project_id=episode.project_id,
            asset_type="TIMELINE",
            name=f"第{episode.episode_no}集 · 时间轴",
            created_by=actor,
            metadata_json={"episode_id": str(episode.id), "timeline_registry": True},
        )
        db.add(asset)
        db.flush()
    if asset.status == "ARCHIVED":
        raise DomainError("ASSET_ARCHIVED", "归档时间轴只允许查看", 409)
    sources = {clip.source_asset_version_id for track in snapshot.tracks for clip in track.clips}
    sources.update(
        clip.audio_version_id
        for track in snapshot.tracks
        for clip in track.clips
        if clip.audio_version_id
    )
    if len(sources) > 63:
        raise DomainError("TIMELINE_INPUT_LIMIT", "时间轴精确媒体输入最多63个", 422)
    manifest, _ = capture_manifest(
        db,
        episode.project_id,
        actor,
        ManifestCreate.model_validate(
            {
                "creation_key": uuid5(key, "timeline-inputs:" + str(episode.id)),
                "target_asset_id": asset.id,
                "inputs": [
                    {
                        "source_asset_version_id": snapshot.storyboard_id,
                        "dependency_type": "SEQUENCE_DEPENDENCY",
                    },
                    *[
                        {
                            "source_asset_version_id": identity,
                            "dependency_type": "RENDER_DEPENDENCY",
                        }
                        for identity in sorted(sources)
                    ],
                ],
                "context": {
                    "episode_id": str(episode.id),
                    "timeline_schema_version": snapshot.schema_version,
                },
            }
        ),
    )
    number = (
        db.scalar(select(func.max(Timeline.version_no)).where(Timeline.episode_id == episode.id))
        or 0
    ) + 1
    if base and number != base.version_no + 1:
        raise DomainError("VERSION_CONFLICT", "时间轴已有新版本，请保留草稿并重新载入", 409)
    metadata = {
        "episode_id": str(episode.id),
        "timeline_registry": True,
        "dependency_manifest_id": str(manifest.id),
        "timeline_schema_version": snapshot.schema_version,
    }
    if base:
        metadata["based_on_timeline_id"] = str(base.id)
    version = AssetVersion(
        id=uuid4(),
        asset_id=asset.id,
        version_no=number,
        object_key="database:timeline:" + str(key),
        checksum=digest(snapshot.model_dump(mode="json")),
        created_by=actor,
        creation_key=key,
        metadata_json=metadata,
    )
    db.add(version)
    db.flush()
    row = Timeline(
        id=version.id,
        episode_id=episode.id,
        storyboard_id=snapshot.storyboard_id,
        version_no=number,
        default_duration_ms=snapshot.default_duration_ms,
        duration_ms=snapshot.duration_ms,
        output_profile_json=deepcopy(snapshot.output_profile),
        creation_key=key,
        request_sha256=request_hash,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    for track in snapshot.tracks:
        db.add(
            TimelineTrack(id=track.id, timeline_id=row.id, kind=track.kind, ordinal=track.ordinal)
        )
    db.flush()
    for track in snapshot.tracks:
        for clip in track.clips:
            db.add(
                TimelineClip(
                    timeline_id=row.id, track_id=track.id, **clip.model_dump(exclude={"visual"})
                )
            )
    db.flush()
    for track in snapshot.tracks:
        for clip in track.clips:
            if clip.visual is not None:
                db.add(
                    TimelineClipVisual(
                        id=clip.id,
                        timeline_id=row.id,
                        settings_json=clip.visual.model_dump(mode="json"),
                    )
                )
    db.flush()
    row.sealed = True
    db.flush()
    attach_output(db, episode.project_id, manifest.id, version)
    if base:
        db.add(
            TimelineRevision(
                id=row.id,
                based_on_timeline_id=base.id,
                expected_version_no=base.version_no,
                created_by=actor,
            )
        )
        db.flush()
    audit(
        db,
        episode.project_id,
        actor,
        "timeline.saved" if base else "timeline.created",
        row.id,
        {
            "episode_id": str(episode.id),
            "version_no": number,
            "content_sha256": version.checksum,
            **({"based_on_timeline_id": str(base.id)} if base else {}),
        },
    )
    return read_timeline(db, row.id, actor)
