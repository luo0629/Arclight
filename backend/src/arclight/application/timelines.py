"""Create frozen default timelines through existing selection, QA and dependency gates."""

from copy import deepcopy
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, ShotContent
from arclight.application.asset_objects import read_object
from arclight.application.commands import require_episode
from arclight.application.continuity_selection import require_shot_review, validate_selected_shot
from arclight.application.dependencies import (
    exact_version,
    lock_dependency_graph,
)
from arclight.application.timeline_store import append_timeline, read_timeline, replay_timeline
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.timelines import (
    TRACK_KINDS,
    ClipSnapshot,
    TimelineCreate,
    TimelineRead,
    TimelineSave,
    TimelineSnapshot,
    TrackSnapshot,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    Episode,
    Project,
    Script,
    Shot,
    Storyboard,
)
from arclight.persistence.timelines import Timeline


def write_episode(db: Session, episode_id: UUID, actor: str) -> Episode:
    episode = require_episode(db, episode_id, actor, write=True)
    lock_dependency_graph(db, episode.project_id)
    db.refresh(episode)
    return require_episode(db, episode_id, actor, write=True)


def approved_board(db: Session, episode: Episode, board_id: UUID) -> tuple[Storyboard, list[Shot]]:
    board = db.get(Storyboard, board_id, populate_existing=True)
    if board is None or board.episode_id != episode.id:
        raise DomainError("NOT_FOUND", "本剧集的精确分镜不存在", 404)
    registry, _ = exact_version(db, episode.project_id, board.id)
    if board.status != "APPROVED" or registry.status != "APPROVED" or registry.stale:
        raise DomainError("STORYBOARD_NOT_APPROVED", "时间轴需要已批准且有效的精确分镜", 409)
    shots = list(
        db.scalars(
            select(Shot).where(Shot.storyboard_id == board.id).order_by(Shot.shot_no).limit(64)
        )
    )
    return board, shots


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
    episode = write_episode(db, episode_id, actor)
    request_hash = digest(
        {
            **payload.model_dump(mode="json", exclude={"creation_key", "selected_shots"}),
            "selected_shots": sorted(
                [item.model_dump(mode="json") for item in payload.selected_shots],
                key=lambda item: item["shot_id"],
            ),
        }
    )
    replay = replay_timeline(db, episode.id, payload.creation_key, request_hash, actor)
    if replay:
        return replay
    board, shots = approved_board(db, episode, payload.storyboard_id)
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
    return append_timeline(db, episode, actor, payload.creation_key, request_hash, snapshot)


def canonical_snapshot(value: TimelineSnapshot) -> TimelineSnapshot:
    return value.model_copy(
        update={
            "tracks": [
                track.model_copy(
                    update={"clips": sorted(track.clips, key=lambda clip: clip.ordinal)}
                )
                for track in sorted(value.tracks, key=lambda item: item.ordinal)
            ]
        }
    )


def require_track_source(
    db: Session, episode: Episode, identity: UUID, kind: str, checksum: str | None = None
) -> None:
    version, asset = exact_version(db, episode.project_id, identity)
    if (
        asset.asset_type != kind
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or version.status not in {"APPROVED", "LOCKED"}
        or version.stale
        or (checksum is not None and version.checksum != checksum)
    ):
        raise DomainError("ASSET_NOT_USABLE", "时间轴轨道需要已批准且有效的精确素材版本", 409)


def validate_snapshot(
    db: Session, settings: Settings, episode: Episode, value: TimelineSnapshot
) -> None:
    if value.episode_id != episode.id:
        raise DomainError("TIMELINE_SCOPE_INVALID", "时间轴内容必须属于当前剧集", 422)
    board, shots = approved_board(db, episode, value.storyboard_id)
    video = next(track for track in value.tracks if track.kind == "VIDEO")
    if (
        len(shots) > 63
        or len(video.clips) != len(shots)
        or {clip.shot_id for clip in video.clips} != {shot.id for shot in shots}
    ):
        raise DomainError(
            "TIMELINE_SHOTS_INCOMPLETE", "时间轴必须保留本分镜全部镜头的精确选用", 422
        )
    project = db.get(Project, episode.project_id)
    assert project is not None
    if value.output_profile != project.output_profile:
        raise DomainError("TIMELINE_PROFILE_CONFLICT", "请载入当前项目输出规格后再保存", 409)
    shots_by_id = {shot.id: shot for shot in shots}
    script = db.get(Script, board.script_version_id)
    assert script is not None
    script_content = ScriptContent.model_validate(script.content_json)
    line_ids = {line.line_id for scene in script_content.scenes for line in scene.dialogue}
    for track in value.tracks:
        for clip in track.clips:
            if clip.shot_id is not None and clip.shot_id not in shots_by_id:
                raise DomainError("TIMELINE_SCOPE_INVALID", "轨道片段不能引用其他分镜的镜头", 422)
            if track.kind != "VIDEO" and any(
                (clip.review_id, clip.review_storyboard_id, clip.review_shot_id)
            ):
                raise DomainError(
                    "TIMELINE_REFERENCE_CONFLICT", "视觉审查身份只用于其精确画面片段", 409
                )
            if track.kind == "VIDEO":
                assert clip.shot_id is not None
                expected = selected_clip(
                    db,
                    settings,
                    episode,
                    shots_by_id[clip.shot_id],
                    clip.source_asset_version_id,
                    clip.ordinal,
                    clip.duration_ms,
                )
                for name in (
                    "source_sha256",
                    "review_id",
                    "review_storyboard_id",
                    "review_shot_id",
                ):
                    if getattr(clip, name) != getattr(expected, name):
                        raise DomainError(
                            "TIMELINE_REFERENCE_CONFLICT", "片段须保留当前精确素材与审查身份", 409
                        )
            else:
                require_track_source(
                    db,
                    episode,
                    clip.source_asset_version_id,
                    "SUBTITLE" if track.kind == "SUBTITLE" else "AUDIO",
                    clip.source_sha256,
                )
            if clip.line_id is not None and (
                clip.script_version_id != script.id or clip.line_id not in line_ids
            ):
                raise DomainError(
                    "TIMELINE_LINE_INVALID", "片段须引用本精确分镜剧本中的稳定台词身份", 422
                )
            if clip.audio_version_id:
                require_track_source(db, episode, clip.audio_version_id, "AUDIO")


def save_timeline(
    db: Session, settings: Settings, timeline_id: UUID, actor: str, payload: TimelineSave
) -> TimelineRead:
    base = db.get(Timeline, timeline_id)
    if base is None or not base.sealed:
        raise DomainError("NOT_FOUND", "时间轴版本不存在", 404)
    episode = write_episode(db, base.episode_id, actor)
    snapshot = canonical_snapshot(payload.snapshot)
    request_hash = digest(
        {
            "operation": "SAVE",
            "based_on_timeline_id": str(base.id),
            "expected_version_no": payload.expected_version_no,
            "snapshot": snapshot.model_dump(mode="json"),
        }
    )
    replay = replay_timeline(db, episode.id, payload.creation_key, request_hash, actor)
    if replay:
        return replay
    current = db.scalar(
        select(Timeline)
        .where(Timeline.episode_id == episode.id, Timeline.sealed.is_(True))
        .order_by(Timeline.version_no.desc())
        .limit(1)
    )
    if current is None or current.id != base.id or payload.expected_version_no != base.version_no:
        raise DomainError(
            "VERSION_CONFLICT",
            f"时间轴已变化，请保留草稿并载入当前第{current.version_no if current else 0}版",
            409,
        )
    read_timeline(db, base.id, actor)
    validate_snapshot(db, settings, episode, snapshot)
    fresh = snapshot.model_copy(
        update={
            "tracks": [
                track.model_copy(
                    update={
                        "id": uuid4(),
                        "clips": [clip.model_copy(update={"id": uuid4()}) for clip in track.clips],
                    }
                )
                for track in snapshot.tracks
            ]
        }
    )
    return append_timeline(db, episode, actor, payload.creation_key, request_hash, fresh, base=base)
