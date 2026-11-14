"""Compile exact images, audio and captions into the existing sealed render protocol."""

import base64
import re
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.application.render_inputs import file_storage
from arclight.application.subtitles import checked_production, production_record
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import RenderFileReference, RenderManifestSnapshot
from arclight.domain.subtitles import SubtitleCue, visible_cues
from arclight.domain.timeline_visuals import ClipVisual
from arclight.media.execution import MediaObserver
from arclight.media.render_plan import (
    MAX_FRAMES,
    MAX_PIXEL_FRAMES,
    RenderAudioClip,
    RenderAudioSource,
    RenderClip,
    RenderCue,
    RenderPlan,
    RenderSubtitles,
    validate_audio_work,
)
from arclight.media.versions import renderer_software
from arclight.persistence.models import AssetVersion, StoredFile


def check_support(snapshot: RenderManifestSnapshot) -> None:
    if snapshot.software != renderer_software():
        raise DomainError(
            "RENDER_SOFTWARE_CHANGED", "渲染软件版本已变化，请另建清单并重新批准", 409
        )
    frames = (snapshot.timeline.duration_ms * snapshot.output.fps + 999) // 1000
    if (
        frames > MAX_FRAMES
        or frames * snapshot.output.width * snapshot.output.height > MAX_PIXEL_FRAMES
    ):
        raise DomainError("RENDER_WORK_LIMIT", "渲染超出当前受限编码器的帧数或像素工作量", 409)
    ranges = [
        (clip.start_ms, clip.duration_ms, clip.source_start_ms)
        for track in snapshot.timeline.tracks
        if track.enabled and track.kind in {"VOICE", "MUSIC", "SFX"}
        for clip in track.clips
    ]
    try:
        validate_audio_work(ranges)
    except ValueError:
        raise DomainError(
            "RENDER_AUDIO_WORK_LIMIT",
            "音频超出16路并行解码或累计3600秒源解码工作量，请缩短源入点或拆分时间轴",
            409,
        ) from None
    end = 0
    for track in snapshot.timeline.tracks:
        if track.kind != "SUBTITLE" or not track.enabled:
            continue
        for clip in sorted(track.clips, key=lambda item: item.ordinal):
            if clip.start_ms < end:
                raise DomainError(
                    "RENDER_SUBTITLE_RANGE_INVALID", "字幕须按时间排序且不可重叠", 409
                )
            end = clip.start_ms + clip.duration_ms


def exact_file_bytes(db: Session, settings: Settings, reference: RenderFileReference) -> bytes:
    file = db.get(StoredFile, reference.file_id)
    if file is None:
        raise DomainError("OBJECT_UNAVAILABLE", "精确文件回执不存在", 409)
    data = file_storage(settings, file).read(file.object_key, None, reference.sha256)
    if len(data) != reference.byte_size:
        raise DomainError("OBJECT_UNAVAILABLE", "精确素材大小与文件回执不符", 409)
    return data


def legacy_cues(data: bytes) -> tuple[RenderCue, ...] | str:
    """Preserve literal legacy captions; parse timestamped SRT as cues, never as display text."""
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n")
        if "-->" not in text:
            return text
        text = text.strip("\n")
        result = []
        time_pattern = r"(\d{2}):([0-5]\d):([0-5]\d),(\d{3})"
        for index, block in enumerate(text.split("\n\n"), 1):
            number, timing, content = block.split("\n", 2)
            match = re.fullmatch(time_pattern + " --> " + time_pattern, timing)
            if int(number) != index or match is None:
                raise ValueError("Invalid SRT cue")
            values = list(map(int, match.groups()))
            start, end = [
                ((values[i] * 60 + values[i + 1]) * 60 + values[i + 2]) * 1000 + values[i + 3]
                for i in (0, 4)
            ]
            result.append(RenderCue(text=content, start_ms=start, duration_ms=end - start))
        return tuple(result)
    except (UnicodeError, ValueError):
        raise DomainError(
            "RENDER_SUBTITLE_INVALID", "旧字幕不是有效的UTF-8文字或标准SRT", 409
        ) from None


def build_subtitles(
    db: Session,
    settings: Settings,
    snapshot: RenderManifestSnapshot,
    *,
    observer: MediaObserver | None = None,
) -> RenderSubtitles | None:
    track = next(track for track in snapshot.timeline.tracks if track.kind == "SUBTITLE")
    clips = track.clips if track.enabled else []
    if not clips:
        return None
    sources = {item.asset_version_id: item for item in snapshot.sources}
    documents: dict[UUID, tuple[SubtitleCue | RenderCue, ...] | str] = {}
    cues = []
    for clip in sorted(clips, key=lambda item: item.ordinal):
        if observer is not None:
            observer(None)
        if clip.source_asset_version_id not in documents:
            version = db.get(AssetVersion, clip.source_asset_version_id)
            assert version is not None
            production = production_record(db, version)
            if production is not None:
                production_document, _, _, _ = checked_production(db, settings, production)
                documents[version.id] = tuple(visible_cues(production_document))
            else:
                reference = sources[version.id].file
                assert reference is not None
                documents[version.id] = legacy_cues(exact_file_bytes(db, settings, reference))
        document = documents[clip.source_asset_version_id]
        if isinstance(document, str):
            text = document
        else:
            matches = [
                cue
                for cue in document
                if cue.start_ms == clip.start_ms
                and cue.duration_ms == clip.duration_ms
                and (
                    isinstance(cue, RenderCue)
                    or (
                        cue.script_version_id == clip.script_version_id
                        and cue.line_id == clip.line_id
                        and cue.audio_asset_version_id == clip.audio_version_id
                    )
                )
            ]
            if len(matches) != 1:
                raise DomainError(
                    "SUBTITLE_CLIP_MISMATCH", "字幕片段与精确显示文字、时点或台词来源不符", 409
                )
            text = matches[0].text
        cues.append(RenderCue(text=text, start_ms=clip.start_ms, duration_ms=clip.duration_ms))
    assert snapshot.font is not None
    return RenderSubtitles.model_validate(
        {
            "font": base64.b64encode(exact_file_bytes(db, settings, snapshot.font)).decode("ascii"),
            "font_media_type": snapshot.font.media_type,
            "style": snapshot.subtitle_style,
            "cues": tuple(cues),
        }
    )


def build_plan(
    db: Session,
    settings: Settings,
    snapshot: RenderManifestSnapshot,
    *,
    observer: MediaObserver | None = None,
) -> bytes:
    check_support(snapshot)
    sources = {item.asset_version_id: item for item in snapshot.sources}
    fades = snapshot.timeline.video_fades()
    clips = []
    for clip in sorted(
        next(t for t in snapshot.timeline.tracks if t.kind == "VIDEO").clips,
        key=lambda c: c.ordinal,
    ):
        if observer is not None:
            observer(None)
        reference = sources[clip.source_asset_version_id].file
        assert reference is not None
        data = exact_file_bytes(db, settings, reference)
        clips.append(
            RenderClip(
                image=base64.b64encode(data).decode("ascii"),
                start_ms=clip.start_ms,
                duration_ms=clip.duration_ms,
                visual=clip.visual or ClipVisual(),
                fade_in_ms=fades[clip.id].incoming_ms,
                fade_out_ms=fades[clip.id].outgoing_ms,
            )
        )
    audio_sources: list[RenderAudioSource] = []
    audio_indices = {}
    audio_clips = []
    try:
        for track in snapshot.timeline.tracks:
            if not track.enabled or track.kind not in {"VOICE", "MUSIC", "SFX"}:
                continue
            for clip in sorted(track.clips, key=lambda item: item.ordinal):
                if observer is not None:
                    observer(None)
                source_id = clip.source_asset_version_id
                if source_id not in audio_indices:
                    reference = sources[source_id].file
                    assert reference is not None
                    audio_indices[source_id] = len(audio_sources)
                    audio_sources.append(
                        RenderAudioSource.model_validate(
                            {
                                "data": base64.b64encode(
                                    exact_file_bytes(db, settings, reference)
                                ).decode("ascii"),
                                "media_type": reference.media_type,
                            }
                        )
                    )
                audio_clips.append(
                    RenderAudioClip(
                        source_index=audio_indices[source_id],
                        start_ms=clip.start_ms,
                        duration_ms=clip.duration_ms,
                        source_start_ms=clip.source_start_ms,
                        audio=clip.audio,
                    )
                )
        plan = RenderPlan(
            output=snapshot.output,
            duration_ms=snapshot.timeline.duration_ms,
            clips=tuple(clips),
            audio_sources=tuple(audio_sources),
            audio_clips=tuple(audio_clips),
            subtitles=build_subtitles(db, settings, snapshot, observer=observer),
        )
    except ValidationError:
        raise DomainError("RENDER_PLAN_INVALID", "渲染计划超限或时间范围无效", 409) from None
    return plan.model_dump_json().encode()
