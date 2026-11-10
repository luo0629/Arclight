"""Exact creative sound decisions; a missing voice is never implicit silence."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, ShotContent
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import LineAudioDecision
from arclight.domain.timelines import TimelineSnapshot
from arclight.persistence.models import Script, Shot, Storyboard


def validate_line_audio(
    db: Session,
    timeline: TimelineSnapshot,
    decisions: tuple[LineAudioDecision, ...],
    *,
    complete: bool,
) -> None:
    board = db.get(Storyboard, timeline.storyboard_id)
    if board is None:
        raise DomainError("RENDER_INPUT_INVALID", "缺少本精确分镜的来源剧本", 409)
    script = db.get(Script, board.script_version_id)
    if script is None or script.episode_id != timeline.episode_id:
        raise DomainError("RENDER_INPUT_INVALID", "缺少本精确分镜的来源剧本", 409)
    content = ScriptContent.model_validate(script.content_json)
    lines = {(script.id, line.line_id) for scene in content.scenes for line in scene.dialogue}
    tracks = {track.kind: track.clips for track in timeline.tracks}
    voices: dict[tuple[UUID | None, str | None], set[UUID]] = {}
    for clip in tracks["VOICE"]:
        voices.setdefault((clip.script_version_id, clip.line_id), set()).add(
            clip.source_asset_version_id
        )
    subtitles = {(clip.script_version_id, clip.line_id) for clip in tracks["SUBTITLE"]}
    resolved = set()
    for item in decisions:
        identity = (item.script_version_id, item.line_id)
        if identity not in lines or identity in resolved:
            raise DomainError(
                "RENDER_LINE_DECISION_INVALID", "声音决定须唯一关联来源剧本的精确台词", 422
            )
        if identity in voices:
            raise DomainError(
                "RENDER_LINE_DECISION_CONFLICT", "同一台词不能同时配音和声明静音", 409
            )
        if item.mode == "SUBTITLE_ONLY" and identity not in subtitles:
            raise DomainError("RENDER_SUBTITLE_REQUIRED", "仅字幕展示的台词必须有精确字幕片段", 409)
        resolved.add(identity)
    if not complete:
        return
    if lines - (voices.keys() | resolved):
        raise DomainError(
            "RENDER_AUDIO_INCOMPLETE", "全部对白和旁白须有配音或明确静音／仅字幕决定", 409
        )
    for clip in tracks["SUBTITLE"]:
        audio_ids = voices.get((clip.script_version_id, clip.line_id), set())
        if (audio_ids and clip.audio_version_id not in audio_ids) or (
            clip.audio_version_id is not None and clip.audio_version_id not in audio_ids
        ):
            raise DomainError(
                "RENDER_SUBTITLE_AUDIO_CONFLICT", "字幕须引用同一精确台词实际使用的配音版本", 409
            )
    for shot in db.scalars(select(Shot).where(Shot.storyboard_id == board.id)):
        if ShotContent.model_validate(shot.content_json).sound_effects and not any(
            clip.shot_id == shot.id for clip in tracks["SFX"]
        ):
            raise DomainError(
                "RENDER_SFX_INCOMPLETE", "分镜已配置的必需音效须有对应镜头的音效片段", 409
            )
