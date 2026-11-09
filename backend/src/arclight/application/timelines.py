"""Create frozen default timelines through existing selection, QA and dependency gates."""

from copy import deepcopy
from datetime import UTC
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContent
from arclight.application.asset_objects import read_object
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.continuity_selection import require_shot_review, validate_selected_shot
from arclight.application.dependencies import (
    attach_output,
    capture_manifest,
    exact_version,
    lock_dependency_graph,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.dependencies import ManifestCreate
from arclight.domain.timelines import (
    TRACK_KINDS,
    ClipSnapshot,
    TimelineCreate,
    TimelineRead,
    TimelineSnapshot,
    TrackSnapshot,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Project,
    Shot,
    Storyboard,
    VisualAsset,
)
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack


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
    snapshot = TimelineSnapshot(
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
                        {key: getattr(clip, key) for key in ClipSnapshot.model_fields}
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
    return TimelineRead(
        id=row.id,
        asset_id=asset.id,
        version_no=row.version_no,
        status=version.status,
        stale=version.stale,
        content_sha256=version.checksum,
        dependency_manifest_id=UUID(version.metadata_json["dependency_manifest_id"]),
        created_at=(
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None
            else row.created_at.astimezone(UTC)
        ),
        snapshot=snapshot,
    )


def selected_clip(
    db: Session,
    settings: Settings,
    episode: Episode,
    shot: Shot,
    version_id: UUID,
    ordinal: int,
    duration_ms: int,
) -> ClipSnapshot:
    version, asset = exact_version(db, episode.project_id, version_id)
    if (
        asset.asset_type not in {"IMAGE", "SHOT_IMAGE"}
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or asset.current_version_id != version.id
        or version.status not in {"APPROVED", "LOCKED"}
        or version.stale
    ):
        raise DomainError("ASSET_NOT_USABLE", "时间轴只能使用当前已选用且已批准的有效镜头图", 409)
    record = require_shot_review(db, version)
    try:
        content = ShotContent.model_validate(shot.content_json)
    except ValidationError as exc:
        raise DomainError("INVALID_STORYBOARD", "镜头内容不符合当前合同", 409) from exc
    validate_selected_shot(db, record, content, episode.id)
    if record.review.target.shot_id != shot.id and (
        content.quality_review is None
        or content.quality_review.review_id != str(record.review.result_id)
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "镜头必须保留其精确审查身份", 409)
    if content.selected_visual_asset and (
        content.selected_visual_asset.asset_id != str(asset.id)
        or content.selected_visual_asset.asset_version_id != str(version.id)
    ):
        raise DomainError("TIMELINE_SELECTION_CONFLICT", "镜头已保存的选图与时间轴输入不一致", 409)
    read_object(settings, episode.project_id, version.id, version.object_key, version.checksum)
    return ClipSnapshot(
        id=uuid4(),
        ordinal=ordinal,
        source_asset_version_id=version.id,
        source_sha256=version.checksum,
        start_ms=ordinal * duration_ms,
        duration_ms=duration_ms,
        shot_id=shot.id,
        review_id=record.review.result_id,
        review_storyboard_id=record.review.target.storyboard_id,
        review_shot_id=record.review.target.shot_id,
    )


def create_timeline(
    db: Session, settings: Settings, episode_id: UUID, actor: str, payload: TimelineCreate
) -> TimelineRead:
    episode = require_episode(db, episode_id, actor, write=True)
    lock_dependency_graph(db, episode.project_id)
    db.refresh(episode)
    require_episode(db, episode_id, actor, write=True)
    request_hash = digest(
        {
            **payload.model_dump(mode="json", exclude={"creation_key", "selected_shots"}),
            "selected_shots": sorted(
                [item.model_dump(mode="json") for item in payload.selected_shots],
                key=lambda item: item["shot_id"],
            ),
        }
    )
    previous = db.scalar(
        select(Timeline).where(
            Timeline.episode_id == episode.id, Timeline.creation_key == payload.creation_key
        )
    )
    if previous:
        if previous.request_sha256 != request_hash:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一创建键不能用于不同时间轴输入", 409)
        return read_timeline(db, previous.id, actor)
    board = db.get(Storyboard, payload.storyboard_id, populate_existing=True)
    if board is None or board.episode_id != episode.id:
        raise DomainError("NOT_FOUND", "本剧集的精确分镜不存在", 404)
    registry, _ = exact_version(db, episode.project_id, board.id)
    if board.status != "APPROVED" or registry.status != "APPROVED" or registry.stale:
        raise DomainError("STORYBOARD_NOT_APPROVED", "创建时间轴需要已批准且有效的精确分镜", 409)
    shots = list(
        db.scalars(
            select(Shot).where(Shot.storyboard_id == board.id).order_by(Shot.shot_no).limit(64)
        )
    )
    selections = {item.shot_id: item.asset_version_id for item in payload.selected_shots}
    if len(shots) > 63 or set(selections) != {shot.id for shot in shots}:
        raise DomainError(
            "TIMELINE_SHOTS_INCOMPLETE", "须提供本分镜全部镜头的精确选用，最多63镜头", 422
        )
    clips = [
        selected_clip(
            db, settings, episode, shot, selections[shot.id], index, payload.default_duration_ms
        )
        for index, shot in enumerate(shots)
    ]
    project = db.get(Project, episode.project_id)
    assert project is not None
    snapshot = TimelineSnapshot(
        episode_id=episode.id,
        storyboard_id=board.id,
        default_duration_ms=payload.default_duration_ms,
        duration_ms=len(shots) * payload.default_duration_ms,
        output_profile=deepcopy(project.output_profile),
        tracks=[
            TrackSnapshot(
                id=uuid4(), kind=kind, ordinal=index, clips=clips if kind == "VIDEO" else []
            )
            for index, kind in enumerate(TRACK_KINDS)
        ],
    )
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
    manifest, _ = capture_manifest(
        db,
        episode.project_id,
        actor,
        ManifestCreate.model_validate(
            {
                "creation_key": uuid5(payload.creation_key, "timeline-inputs:" + str(episode.id)),
                "target_asset_id": asset.id,
                "inputs": [
                    {"source_asset_version_id": board.id, "dependency_type": "SEQUENCE_DEPENDENCY"},
                    *[
                        {
                            "source_asset_version_id": identity,
                            "dependency_type": "RENDER_DEPENDENCY",
                        }
                        for identity in sorted(set(selections.values()))
                    ],
                ],
                "context": {"episode_id": str(episode.id), "timeline_schema_version": 1},
            }
        ),
    )
    number = (
        db.scalar(select(func.max(Timeline.version_no)).where(Timeline.episode_id == episode.id))
        or 0
    ) + 1
    version = AssetVersion(
        id=uuid4(),
        asset_id=asset.id,
        version_no=number,
        object_key="database:timeline:" + str(payload.creation_key),
        checksum=digest(snapshot.model_dump(mode="json")),
        created_by=actor,
        creation_key=payload.creation_key,
        metadata_json={
            "episode_id": str(episode.id),
            "timeline_registry": True,
            "dependency_manifest_id": str(manifest.id),
        },
    )
    db.add(version)
    db.flush()
    row = Timeline(
        id=version.id,
        episode_id=episode.id,
        storyboard_id=board.id,
        version_no=number,
        default_duration_ms=snapshot.default_duration_ms,
        duration_ms=snapshot.duration_ms,
        output_profile_json=snapshot.output_profile,
        creation_key=payload.creation_key,
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
            db.add(TimelineClip(timeline_id=row.id, track_id=track.id, **clip.model_dump()))
    db.flush()
    row.sealed = True
    db.flush()
    attach_output(db, episode.project_id, manifest.id, version)
    audit(
        db,
        episode.project_id,
        actor,
        "timeline.created",
        row.id,
        {"episode_id": str(episode.id), "version_no": number, "content_sha256": version.checksum},
    )
    return read_timeline(db, row.id, actor)
