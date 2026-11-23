"""Finish one exact short-drama production through the existing media services."""

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_shot_content
from arclight.application import render_admission
from arclight.application.audio_production import (
    approved_script,
    capture_snapshot,
    validate_audio_line,
)
from arclight.application.audit import audit
from arclight.application.commands import require_episode
from arclight.application.human_reviews import require_review_actor
from arclight.application.production_versions import bind, find_binding
from arclight.application.render_inputs import font_facts
from arclight.application.render_jobs import create_job
from arclight.application.render_manifests import create_render_manifest
from arclight.application.render_reports import report_warnings
from arclight.application.short_drama_budget import root_for_job
from arclight.application.subtitles import generate_subtitles, require_subtitle_version
from arclight.application.timelines import approved_board, create_timeline, save_timeline
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import next_status
from arclight.domain.audio_production import AudioLine, AudioSnapshotCreate
from arclight.domain.render_jobs import RenderJobCreate, RenderJobRead
from arclight.domain.render_manifests import RenderManifestCreate
from arclight.domain.subtitles import SubtitleGenerate
from arclight.domain.timelines import (
    ClipSnapshot,
    CurrentTimelineSnapshot,
    ShotSelection,
    TimelineCreate,
    TimelineRead,
    TimelineSave,
    TrackKind,
)
from arclight.domain.versions import digest
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Episode,
    GenerationJob,
    Shot,
    StoredFile,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)
from arclight.persistence.render_manifests import RenderManifest

AUTOMATIC_EXPORT = "SHORT_DRAMA_EXPORT"


def render_key(run_id: UUID) -> UUID:
    return uuid5(run_id, "short-drama:render")


def manifest_key(run_id: UUID) -> UUID:
    return uuid5(run_id, "short-drama:manifest")


def audio_in_scope(db: Session, version: AssetVersion, run_id: UUID | None) -> bool:
    receipt = version.metadata_json.get("audio_tts")
    if not isinstance(receipt, dict):
        return True
    try:
        job_id = UUID(receipt["job_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    job = db.get(GenerationJob, job_id)
    if job is None or version.creation_key != uuid5(job_id, "audio-tts-result"):
        return False
    scope = root_for_job(db, job)
    return scope is None or scope.id == run_id


def approved_audio(
    db: Session, run_id: UUID | None, snapshot: AudioSnapshot, line_id: str, version_id: UUID | None
) -> tuple[AudioLineBinding, AssetVersion] | None:
    query = (
        select(AudioLineBinding, AssetVersion)
        .join(AssetVersion, AssetVersion.id == AudioLineBinding.asset_version_id)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .join(AudioSnapshot, AudioSnapshot.id == AudioLineBinding.snapshot_id)
        .where(
            AudioSnapshot.episode_id == snapshot.episode_id,
            AudioLineBinding.line_id == line_id,
            AssetVersion.status.in_(("APPROVED", "LOCKED")),
            AssetVersion.stale.is_(False),
            VisualAsset.status.not_in(("ARCHIVED", "REJECTED", "STALE")),
        )
    )
    if version_id is None:
        query = query.where(AudioSnapshot.script_version_id == snapshot.script_version_id)
    else:
        query = query.where(AssetVersion.id == version_id)
    for binding, version in db.execute(
        query.order_by(AudioLineBinding.created_at.desc(), AudioLineBinding.id)
    ):
        if audio_in_scope(db, version, run_id):
            validate_audio_line(db, version.id, snapshot.script_version_id, line_id)
            return binding, version
    return None


def voice_clips(
    db: Session,
    run_id: UUID,
    timeline: TimelineRead,
    snapshot: AudioSnapshot,
    shots: Sequence[Shot],
    audio_versions: Mapping[str, UUID],
) -> list[ClipSnapshot]:
    lines = {line.line_id: line for line in map(AudioLine.model_validate, snapshot.lines_json)}
    if not set(audio_versions) <= lines.keys():
        raise DomainError("SHORT_DRAMA_AUDIO_SCOPE", "配音输入须引用本剧本的精确台词", 422)
    if len(lines) > 128:
        raise DomainError("SHORT_DRAMA_AUDIO_LIMIT", "单集自动字幕最多支持 128 句台词", 422)
    tracks = {track.kind: track for track in timeline.snapshot.tracks}
    voices = list(tracks["VOICE"].clips)
    synced = {(clip.shot_id, clip.line_id): clip for clip in voices}
    placed: set[str] = set()
    visual = {clip.shot_id: clip for clip in tracks["VIDEO"].clips}
    for shot in shots:
        picture = visual[shot.id]
        cursor = picture.start_ms
        shot_lines = read_shot_content(shot.content_json).dialogue_line_ids
        for line_id in shot_lines:
            sync = synced.get((shot.id, line_id))
            if sync is not None:
                if (
                    line_id in placed
                    or sync.start_ms < cursor
                    or (
                        line_id in audio_versions
                        and audio_versions[line_id] != sync.source_asset_version_id
                    )
                ):
                    raise DomainError(
                        "SHORT_DRAMA_AUDIO_SYNC", "配音安排与镜头口型校正的精确台词或时点冲突", 409
                    )
                cursor = sync.start_ms + sync.duration_ms
                placed.add(line_id)
                continue
            if line_id in placed:
                continue
            approved = approved_audio(db, run_id, snapshot, line_id, audio_versions.get(line_id))
            if approved is None:
                raise DomainError("SHORT_DRAMA_AUDIO_REQUIRED", "仍缺少本剧本台词的已批准配音", 409)
            binding, version = approved
            end = cursor + binding.duration_ms
            next_start = min(
                (
                    clip.start_ms
                    for clip in voices
                    if clip.shot_id == shot.id and clip.start_ms >= cursor
                ),
                default=picture.start_ms + picture.duration_ms,
            )
            if end > next_start:
                raise DomainError(
                    "SHORT_DRAMA_AUDIO_DURATION", f"镜头 {shot.shot_no} 的配音无法放入画面时长", 409
                )
            voices.append(
                ClipSnapshot(
                    id=uuid5(run_id, f"short-drama:voice:{line_id}"),
                    ordinal=len(voices),
                    source_asset_version_id=version.id,
                    source_sha256=version.checksum,
                    shot_id=shot.id,
                    script_version_id=snapshot.script_version_id,
                    line_id=line_id,
                    start_ms=cursor,
                    duration_ms=binding.duration_ms,
                )
            )
            placed.add(line_id)
            cursor = end
    if lines.keys() - placed:
        raise DomainError("SHORT_DRAMA_AUDIO_COVERAGE", "分镜尚未为全部剧本台词安排镜头", 409)
    return [
        clip.model_copy(update={"ordinal": index})
        for index, clip in enumerate(sorted(voices, key=lambda item: (item.start_ms, item.ordinal)))
    ]


def save_tracks(
    db: Session,
    settings: Settings,
    actor: str,
    timeline: TimelineRead,
    key: UUID,
    clips: Mapping[TrackKind, Sequence[ClipSnapshot]],
) -> TimelineRead:
    tracks = [
        track.model_copy(update={"clips": list(clips[track.kind])})
        if track.kind in clips
        else track
        for track in timeline.snapshot.tracks
    ]
    try:
        snapshot = CurrentTimelineSnapshot.model_validate(
            timeline.snapshot.model_dump(exclude={"tracks"}) | {"tracks": tracks}
        )
    except ValueError as exc:
        raise DomainError(
            "SHORT_DRAMA_TIMELINE_INVALID", "自动编排的轨道不符合时间轴限制", 422
        ) from exc
    return save_timeline(
        db,
        settings,
        timeline.id,
        actor,
        TimelineSave(creation_key=key, expected_version_no=timeline.version_no, snapshot=snapshot),
    )


def approve_generated_subtitle(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    version_id: UUID,
    actor: str,
    project_id: UUID,
) -> None:
    version = db.get(AssetVersion, version_id, populate_existing=True)
    assert version is not None
    asset = db.get(VisualAsset, version.asset_id)
    assert asset is not None
    require_subtitle_version(db, settings, project_id, asset, version, actor)
    if version.status in {"APPROVED", "LOCKED"}:
        return
    previous = version.status
    if previous == "DRAFT":
        version.status = next_status(previous, "READY_FOR_REVIEW", version.archived_from_status)
        version.revision += 1
    version.status = next_status(version.status, "APPROVED", version.archived_from_status)
    version.revision += 1
    audit(
        db,
        project_id,
        actor,
        "short_drama.subtitle_approved",
        version.id,
        {"run_id": str(run.id), "from": previous, "to": version.status},
    )
    db.flush()


def automatic_export_snapshot(
    run: WorkflowRun, row: RenderManifest, facts: render_admission.RenderFacts
) -> dict:
    return {
        "run_id": str(run.id),
        "authorization_sha256": digest(run.input_json["short_drama"]),
        "actor": run.input_json["created_by"],
        "request_sha256": run.request_fingerprint,
        "script_version_id": str(run.input_json["short_drama"]["script_version_id"]),
        "storyboard_id": str(facts.snapshot.timeline.storyboard_id),
        "manifest_id": str(row.id),
        "manifest_sha256": row.content_sha256,
        "input_sha256": facts.input_sha256,
    }


def require_run_authorization(db: Session, run: WorkflowRun) -> tuple[Episode, str, dict]:
    spec = run.input_json.get("short_drama")
    actor = run.input_json.get("created_by")
    if (
        run.workflow_type != "producer"
        or run.status not in {"RUNNING", "SUCCEEDED"}
        or not isinstance(spec, dict)
        or not isinstance(actor, str)
    ):
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "缺少本次自动制作授权", 409)
    episode = require_episode(db, run.episode_id, actor, write=True, owner=True)
    require_review_actor(db, episode.project_id, actor, owner=True)
    authorized = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == "short_drama.authorized",
            AuditEvent.project_id == episode.project_id,
            AuditEvent.resource_id == run.id,
            AuditEvent.actor_id == actor,
        )
        .limit(1)
    )
    if authorized is None or authorized.details_json != spec:
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "缺少匹配的自动制作授权记录", 409)
    return episode, actor, spec


def require_automatic_export(
    db: Session, row: RenderManifest, facts: render_admission.RenderFacts
) -> WorkflowRun | None:
    grant = find_binding(db, row.project_id, AUTOMATIC_EXPORT, row.id)
    if grant is None:
        return None
    invalid = DomainError("SHORT_DRAMA_EXPORT_INVALID", "自动导出授权与当前制作证据不一致", 409)
    try:
        run_id = UUID(grant.snapshot_json["run_id"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise invalid from exc
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    if run is None:
        raise invalid
    episode, actor, spec = require_run_authorization(db, run)
    from arclight.application.short_drama import require_exact_inputs

    require_exact_inputs(db, episode, spec)
    board = db.get(Storyboard, facts.snapshot.timeline.storyboard_id)
    if (
        actor != row.created_by
        or episode.project_id != row.project_id
        or run.episode_id != facts.snapshot.timeline.episode_id
        or facts.snapshot.output.name != "FORMAL"
        or row.creation_key != manifest_key(run.id)
        or spec.get("font_file_id") != (str(row.font_file_id) if row.font_file_id else None)
        or spec.get("output_profile") != facts.snapshot.timeline.output_profile
        or board is None
        or str(board.script_version_id) != spec.get("script_version_id")
        or grant.snapshot_json != automatic_export_snapshot(run, row, facts)
    ):
        raise invalid
    require_automatic_media_rights(db, run, row.project_id, facts)
    return run


def require_automatic_media_rights(
    db: Session, run: WorkflowRun, project_id: UUID, facts: render_admission.RenderFacts
) -> None:
    pictures = {
        clip.source_asset_version_id: clip
        for track in facts.snapshot.timeline.tracks
        if track.kind == "VIDEO"
        for clip in track.clips
    }
    for source in facts.snapshot.sources:
        if source.asset_type == "AUDIO":
            version = db.get(AssetVersion, source.asset_version_id)
            if (
                version is None
                or source.file is None
                or not source.file.rights_confirmed
                or not audio_in_scope(db, version, run.id)
            ):
                raise DomainError("SHORT_DRAMA_AUDIO_RIGHTS", "配音授权范围不包含本次短剧", 409)
        elif (
            source.asset_type in {"IMAGE", "SHOT_IMAGE", "VIDEO"}
            and source.file is not None
            and not source.file.rights_confirmed
        ):
            from arclight.application.short_drama_visuals import VISUAL_RIGHTS, visual_authorized

            version = db.get(AssetVersion, source.asset_version_id)
            file = db.get(StoredFile, source.file.file_id)
            binding = find_binding(db, project_id, VISUAL_RIGHTS, source.asset_version_id)
            clip = pictures.get(source.asset_version_id)
            if (
                version is None
                or file is None
                or binding is None
                or clip is None
                or binding.snapshot_json.get("purpose") != "FINAL_SHOT"
                or binding.snapshot_json.get("target")
                != {
                    "storyboard_id": str(facts.snapshot.timeline.storyboard_id),
                    "shot_id": str(clip.shot_id),
                }
                or not visual_authorized(db, project_id, version, file, run.id)
            ):
                raise DomainError(
                    "SHORT_DRAMA_VISUAL_RIGHTS", "镜头素材缺少本次短剧的使用授权", 409
                )


def queue_short_drama_delivery(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    storyboard_id: UUID,
    selected_shots: Sequence[ShotSelection],
    *,
    voice_asset_versions: Mapping[str, UUID] | None = None,
    music_clips: Sequence[ClipSnapshot] = (),
    sound_clips: Sequence[ClipSnapshot] = (),
) -> RenderJobRead:
    """Assemble an exact reviewed picture and licensed dialogue, then enqueue formal render."""
    episode, actor, spec = require_run_authorization(db, run)
    script_id = UUID(spec["script_version_id"])
    font_id = UUID(spec["font_file_id"]) if spec.get("font_file_id") else None
    script = approved_script(db, episode, script_id, actor)
    board, shots = approved_board(db, episode, storyboard_id)
    if board.script_version_id != script.id:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "分镜须来自本次确认的精确剧本", 409)
    shot_durations = {shot.id: shot.duration_ms for shot in shots}
    if any(
        item.video is not None and item.video.duration_ms != shot_durations.get(item.shot_id)
        for item in selected_shots
    ):
        raise DomainError("SHORT_DRAMA_SHOT_DURATION", "自动制作须使用分镜中确定的镜头时长", 409)
    audio = capture_snapshot(
        db,
        episode.id,
        actor,
        AudioSnapshotCreate(
            creation_key=uuid5(run.id, "short-drama:audio"), script_version_id=script.id
        ),
    )
    if digest(script.content_json) != spec.get("script_sha256"):
        raise DomainError("SHORT_DRAMA_SCRIPT_CHANGED", "制作授权的剧本摘要已变化", 409)
    font_facts(db, episode.project_id, font_id, required=audio.line_count > 0)
    timeline = create_timeline(
        db,
        settings,
        episode.id,
        actor,
        TimelineCreate(
            creation_key=uuid5(run.id, "short-drama:picture"),
            storyboard_id=board.id,
            selected_shots=list(selected_shots),
        ),
        use_storyboard_durations=True,
    )
    if timeline.snapshot.output_profile != spec.get("output_profile"):
        raise DomainError("SHORT_DRAMA_PROFILE_CHANGED", "项目输出规格已不同于本次制作授权", 409)
    voices = voice_clips(db, run.id, timeline, audio, shots, voice_asset_versions or {})
    if voices or music_clips or sound_clips:
        timeline = save_tracks(
            db,
            settings,
            actor,
            timeline,
            uuid5(run.id, "short-drama:sound"),
            {"VOICE": voices, "MUSIC": music_clips, "SFX": sound_clips},
        )
    if voices:
        subtitles = generate_subtitles(
            db,
            settings,
            episode.id,
            SubtitleGenerate(
                creation_key=uuid5(run.id, "short-drama:subtitles"), timeline_id=timeline.id
            ),
            actor,
        )
        approve_generated_subtitle(db, settings, run, subtitles.id, actor, episode.project_id)
        timeline = save_tracks(
            db,
            settings,
            actor,
            timeline,
            uuid5(run.id, "short-drama:captioned"),
            {"SUBTITLE": subtitles.clips},
        )
    manifest = create_render_manifest(
        db,
        settings,
        timeline.id,
        actor,
        RenderManifestCreate(
            creation_key=manifest_key(run.id), profile="FORMAL", font_file_id=font_id
        ),
    )
    row = db.get(RenderManifest, manifest.id)
    assert row is not None
    facts = render_admission.admission_facts(db, row)
    if report_warnings(db, facts.snapshot):
        raise DomainError(
            "SHORT_DRAMA_EXPORT_WARNINGS", "自动交付要求通过检查且没有待确认例外", 409
        )
    bind(
        db,
        episode.project_id,
        AUTOMATIC_EXPORT,
        manifest.id,
        automatic_export_snapshot(run, row, facts),
    )
    return create_job(
        db, settings, manifest.id, actor, RenderJobCreate(creation_key=render_key(run.id))
    )
