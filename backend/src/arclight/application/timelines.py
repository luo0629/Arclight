"""Create frozen default timelines through existing selection, QA and dependency gates."""

from copy import deepcopy
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContentV3, read_script_content, read_shot_content
from arclight.application.asset_objects import read_object
from arclight.application.commands import require_episode
from arclight.application.continuity_selection import require_shot_review, validate_selected_shot
from arclight.application.dependencies import (
    exact_version,
    lock_dependency_graph,
)
from arclight.application.lip_sync_admission import lip_sync_receipt
from arclight.application.text_reviews import require_performance_production
from arclight.application.timeline_store import append_timeline, read_timeline, replay_timeline
from arclight.application.video_candidates import require_video_version
from arclight.application.video_review import video_review_facts
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.timelines import (
    TRACK_KINDS,
    ClipSnapshot,
    ClipVideo,
    CurrentTimelineSnapshot,
    TimelineCreate,
    TimelineRead,
    TimelineSave,
    TimelineSnapshot,
    TrackSnapshot,
    VideoTrim,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Project,
    Script,
    Shot,
    StoredFile,
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


def verify_visual_object(
    db: Session,
    settings: Settings,
    episode: Episode,
    clip: ClipSnapshot,
    actor: str,
) -> None:
    version, asset = exact_version(db, episode.project_id, clip.source_asset_version_id)
    if clip.video is not None:
        require_video_version(db, settings, episode.project_id, asset, version, actor)
    else:
        read_object(settings, episode.project_id, version.id, version.object_key, version.checksum)


def selected_video_clip(
    db: Session,
    episode: Episode,
    shot: Shot,
    version: AssetVersion,
    ordinal: int,
    default_duration_ms: int,
    trim: VideoTrim | None,
    *,
    motion_required: bool,
) -> ClipSnapshot:
    facts = video_review_facts(db, version)
    record, media = facts.record, facts.media
    target = record.report.target
    if (
        record.report.episode_id != episode.id
        or target.storyboard_id != shot.storyboard_id
        or target.shot_id != shot.id
        or shot.status != "APPROVED"
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "视频须经本集本版分镜的精确镜头审查", 409)
    file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if (
        file is None
        or file.project_id != episode.project_id
        or file.object_key != version.object_key
        or file.checksum != media.checksum
        or file.byte_size != media.byte_size
        or file.media_type != media.media_type
    ):
        raise DomainError("OBJECT_UNAVAILABLE", "视频缺少精确私有文件回执", 409)
    if not (file.source_note.strip() and file.rights_note.strip()):
        raise DomainError("RENDER_RIGHTS_REQUIRED", "视频须保留来源与使用权声明", 409)
    sync = lip_sync_receipt(version)
    if trim is None:
        duration_ms = min(default_duration_ms, media.duration_ms)
        if sync:
            duration_ms = max(duration_ms, *(line.end_ms for line in sync.lines))
        trim = VideoTrim(duration_ms=duration_ms)
    end_ms = trim.source_start_ms + trim.duration_ms
    if end_ms > media.duration_ms:
        raise DomainError("TIMELINE_SOURCE_RANGE_INVALID", "视频出点超出精确源视频时长", 409)
    if sync and (
        trim.keep_audio
        or any(line.start_ms < trim.source_start_ms or line.end_ms > end_ms for line in sync.lines)
    ):
        raise DomainError(
            "LIP_SYNC_TIMELINE_MISMATCH",
            "须保留完整口型对白范围，并由配音轨使用精确音频，不能重复混入候选试听轨",
            409,
        )
    if trim.keep_audio and not media.has_audio:
        raise DomainError("TIMELINE_VIDEO_AUDIO_MISSING", "源视频没有可保留的原声轨", 409)
    return ClipSnapshot(
        id=uuid4(),
        ordinal=ordinal,
        source_asset_version_id=version.id,
        source_sha256=version.checksum,
        start_ms=0,
        source_start_ms=trim.source_start_ms,
        duration_ms=trim.duration_ms,
        shot_id=shot.id,
        review_id=record.report.report_id,
        review_storyboard_id=target.storyboard_id,
        review_shot_id=target.shot_id,
        video=ClipVideo(
            candidate_id=target.candidate_id,
            file_id=file.id,
            review_sha256=record.report_sha256,
            human_decision_id=facts.decision.id if facts.decision else None,
            source_end_ms=end_ms,
            source_duration_ms=media.duration_ms,
            frame_rate=media.frame_rate,
            has_audio=media.has_audio,
            keep_audio=trim.keep_audio,
            motion_required=motion_required,
        ),
    )


def selected_clip_facts(
    db: Session,
    episode: Episode,
    shot: Shot,
    version_id: UUID,
    ordinal: int,
    duration_ms: int,
    *,
    trim: VideoTrim | None = None,
) -> ClipSnapshot:
    """Database-only selection/QA checks shared with transactional export reviews."""
    try:
        content = read_shot_content(shot.content_json)
    except ValidationError as exc:
        raise DomainError("INVALID_STORYBOARD", "镜头内容不符合当前合同", 409) from exc
    motion_required = (
        isinstance(content, ShotContentV3)
        and content.performance.motion_requirement == "dynamic_required"
    )
    if isinstance(content, ShotContentV3) and not motion_required:
        require_performance_production(db, shot.storyboard_id)
    try:
        exact = exact_version(db, episode.project_id, version_id)
    except DomainError as exc:
        if not motion_required or exc.code != "NOT_FOUND":
            raise
        exact = None
    if motion_required and (exact is None or exact[1].asset_type != "VIDEO"):
        raise DomainError(
            "DYNAMIC_MEDIA_REQUIRED",
            "必须动态的镜头不能选用静图时间轴，请保留动态要求并修订制作路线。",
            409,
        )
    assert exact is not None
    version, asset = exact
    if motion_required:
        require_performance_production(db, shot.storyboard_id)
    if (
        asset.asset_type not in {"IMAGE", "SHOT_IMAGE", "VIDEO"}
        or asset.status in {"ARCHIVED", "STALE", "REJECTED"}
        or asset.current_version_id != version.id
        or version.status not in {"APPROVED", "LOCKED"}
        or version.stale
    ):
        raise DomainError("ASSET_NOT_USABLE", "时间轴只能使用当前已选用且已批准的镜头素材", 409)
    if asset.asset_type == "VIDEO":
        return selected_video_clip(
            db, episode, shot, version, ordinal, duration_ms, trim, motion_required=motion_required
        )
    if trim is not None:
        raise DomainError("TIMELINE_REFERENCE_CONFLICT", "视频裁切仅用于精确视频版本", 409)
    record = require_shot_review(db, version)
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


def lip_sync_voices(db: Session, clips: list[ClipSnapshot]) -> list[ClipSnapshot]:
    """Keep the exact source audio on VOICE; the candidate audio is for review only."""
    voices: list[ClipSnapshot] = []
    for clip in clips:
        if clip.video is None:
            continue
        version = db.get(AssetVersion, clip.source_asset_version_id)
        assert version is not None
        sync = lip_sync_receipt(version)
        if sync is None:
            continue
        for line in sync.lines:
            if len(voices) >= 256:
                raise DomainError("LIP_SYNC_TIMELINE_LIMIT", "请分段安排超过 256 句的校正对白", 422)
            if clip.visual is not None and any(
                crop.x > line.mouth.x
                or crop.y > line.mouth.y
                or crop.x + crop.width < line.mouth.x + line.mouth.width
                or crop.y + crop.height < line.mouth.y + line.mouth.height
                for crop in (clip.visual.crop, clip.visual.motion.end_crop)
                if crop is not None
            ):
                raise DomainError("LIP_SYNC_TIMELINE_MISMATCH", "画面裁切不能隐藏校正过的口部", 409)
            voice = ClipSnapshot(
                id=uuid4(),
                ordinal=len(voices),
                source_asset_version_id=line.audio_version_id,
                source_sha256=line.audio_sha256,
                shot_id=clip.shot_id,
                script_version_id=sync.script_version_id,
                line_id=line.line_id,
                start_ms=clip.start_ms + line.start_ms - clip.source_start_ms,
                duration_ms=line.duration_ms,
            )
            voices.append(voice)
    return voices


def require_lip_sync_voices(db: Session, tracks: list[TrackSnapshot]) -> None:
    visual = next(track for track in tracks if track.kind == "VIDEO")
    expected = lip_sync_voices(db, visual.clips)
    if not expected or not visual.enabled:
        return
    voice = next(track for track in tracks if track.kind == "VOICE")
    for cue in expected:
        matches = [
            clip
            for clip in voice.clips
            if clip.script_version_id == cue.script_version_id
            and clip.line_id == cue.line_id
            and clip.start_ms < cue.start_ms + cue.duration_ms
            and cue.start_ms < clip.start_ms + clip.duration_ms
        ]
        if (
            not voice.enabled
            or len(matches) != 1
            or any(
                getattr(matches[0], field) != getattr(cue, field)
                for field in (
                    "shot_id",
                    "source_asset_version_id",
                    "source_sha256",
                    "start_ms",
                    "source_start_ms",
                    "duration_ms",
                )
            )
        ):
            raise DomainError(
                "LIP_SYNC_TIMELINE_MISMATCH", "可见对白须同步使用校正时的精确配音版本与时点", 409
            )


def create_timeline(
    db: Session,
    settings: Settings,
    episode_id: UUID,
    actor: str,
    payload: TimelineCreate,
    *,
    use_storyboard_durations: bool = False,
) -> TimelineRead:
    episode = write_episode(db, episode_id, actor)
    request_hash = digest(
        {
            **payload.model_dump(mode="json", exclude={"creation_key", "selected_shots"}),
            "selected_shots": sorted(
                [item.model_dump(mode="json") for item in payload.selected_shots],
                key=lambda item: item["shot_id"],
            ),
            **({"use_storyboard_durations": True} if use_storyboard_durations else {}),
        }
    )
    replay = replay_timeline(db, episode.id, payload.creation_key, request_hash, actor)
    if replay:
        return replay
    board, shots = approved_board(db, episode, payload.storyboard_id)
    selections = {item.shot_id: item for item in payload.selected_shots}
    if len(shots) > 63 or set(selections) != {shot.id for shot in shots}:
        raise DomainError(
            "TIMELINE_SHOTS_INCOMPLETE", "须提供本分镜全部镜头的精确选用，最多63镜头", 422
        )
    clips = []
    cursor = 0
    for index, shot in enumerate(shots):
        selection = selections[shot.id]
        clip = selected_clip_facts(
            db,
            episode,
            shot,
            selection.asset_version_id,
            index,
            shot.duration_ms if use_storyboard_durations else payload.default_duration_ms,
            trim=selection.video,
        )
        verify_visual_object(db, settings, episode, clip, actor)
        clips.append(clip.model_copy(update={"start_ms": cursor, "visual": ClipVisual()}))
        cursor += clip.duration_ms
    project = db.get(Project, episode.project_id)
    assert project is not None
    sources = {"VIDEO": clips, "VOICE": lip_sync_voices(db, clips)}
    snapshot = CurrentTimelineSnapshot(
        episode_id=episode.id,
        storyboard_id=board.id,
        default_duration_ms=payload.default_duration_ms,
        duration_ms=cursor,
        output_profile=deepcopy(project.output_profile),
        tracks=[
            TrackSnapshot(id=uuid4(), kind=kind, ordinal=index, clips=sources.get(kind, []))
            for index, kind in enumerate(TRACK_KINDS)
        ],
    )
    if sources["VOICE"]:
        validate_snapshot_facts(db, episode, snapshot)
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


def require_sound_source(db: Session, episode: Episode, clip: ClipSnapshot) -> None:
    """Preserve legacy AUDIO references while admitting exact registered music resources."""
    version, asset = exact_version(db, episode.project_id, clip.source_asset_version_id)
    kind = "MUSIC" if asset.asset_type == "MUSIC" else "AUDIO"
    require_track_source(db, episode, version.id, kind, clip.source_sha256)
    if kind == "MUSIC":
        from arclight.application.music_assets import music_resource_facts

        resource = music_resource_facts(db, episode.project_id, asset, version)
        if clip.source_start_ms + clip.duration_ms > resource.validation.duration_ms:
            raise DomainError(
                "TIMELINE_SOURCE_RANGE_INVALID", "音乐音效入出点超出精确音频的实测时长", 409
            )
        if asset.current_version_id != version.id:
            raise DomainError("ASSET_NOT_SELECTED", "音乐音效须显式选用本精确批准版本", 409)
        if clip.audio is None or clip.audio.purpose_role != resource.purpose_role:
            raise DomainError(
                "TIMELINE_AUDIO_ROLE_INVALID", "音轨须固定与素材版本一致的音乐或音效用途", 409
            )
    elif clip.audio is not None:
        file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
        if (
            file is None
            or file.project_id != episode.project_id
            or file.checksum != version.checksum
            or file.object_key != version.object_key
            or not file.media_type.startswith("audio/")
            or not file.rights_confirmed
            or not file.source_note.strip()
            or not file.rights_note.strip()
        ):
            raise DomainError("RENDER_RIGHTS_REQUIRED", "音乐音效须保留精确文件来源和使用授权", 409)


def validate_snapshot(
    db: Session, settings: Settings, episode: Episode, value: TimelineSnapshot, *, actor: str
) -> None:
    validate_snapshot_facts(db, episode, value, current_profile=True)
    for track in value.tracks:
        if track.kind == "VIDEO":
            for clip in track.clips:
                verify_visual_object(db, settings, episode, clip, actor)
        elif track.kind in {"MUSIC", "SFX"}:
            from arclight.application.music_assets import require_music_version

            for clip in track.clips:
                version, asset = exact_version(db, episode.project_id, clip.source_asset_version_id)
                if asset.asset_type == "MUSIC":
                    require_music_version(db, settings, episode.project_id, asset, version, actor)


def validate_visual_clip_facts(
    db: Session, episode: Episode, shot: Shot, clip: ClipSnapshot
) -> None:
    expected = selected_clip_facts(
        db,
        episode,
        shot,
        clip.source_asset_version_id,
        clip.ordinal,
        clip.duration_ms,
        trim=VideoTrim(
            source_start_ms=clip.source_start_ms,
            duration_ms=clip.duration_ms,
            keep_audio=clip.video.keep_audio,
        )
        if clip.video
        else None,
    )
    if any(
        getattr(clip, field) != getattr(expected, field)
        for field in (
            "source_sha256",
            "review_id",
            "review_storyboard_id",
            "review_shot_id",
            "video",
        )
    ):
        raise DomainError("TIMELINE_REFERENCE_CONFLICT", "片段须保留当前精确素材与审查身份", 409)


def validate_snapshot_facts(
    db: Session, episode: Episode, value: TimelineSnapshot, *, current_profile: bool = False
) -> None:
    """Validate frozen identities without file I/O or rewriting historical project specs."""
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
    if current_profile and value.output_profile != project.output_profile:
        raise DomainError("TIMELINE_PROFILE_CONFLICT", "请载入当前项目输出规格后再保存", 409)
    shots_by_id = {shot.id: shot for shot in shots}
    script = db.get(Script, board.script_version_id)
    assert script is not None
    script_content = read_script_content(script.content_json)
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
                validate_visual_clip_facts(db, episode, shots_by_id[clip.shot_id], clip)
            elif track.kind in {"MUSIC", "SFX"}:
                require_sound_source(db, episode, clip)
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
            if track.kind == "VOICE":
                from arclight.application.audio_production import validate_audio_line

                assert clip.line_id is not None
                validate_audio_line(db, clip.source_asset_version_id, script.id, clip.line_id)
            if track.kind == "SUBTITLE":
                from arclight.application.subtitles import validate_subtitle_clip

                validate_subtitle_clip(db, episode, clip)
            if clip.audio_version_id:
                require_track_source(db, episode, clip.audio_version_id, "AUDIO")
    require_lip_sync_voices(db, value.tracks)


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
    validate_snapshot(db, settings, episode, snapshot, actor=actor)
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
