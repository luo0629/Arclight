"""Prepare exact dialogue recordings and licensed sound cues before shot generation."""

from collections import Counter
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, read_script_content, read_shot_content
from arclight.application.audio_production import (
    approved_script,
    capture_snapshot,
    script_audio_lines,
)
from arclight.application.audio_tts import (
    create_tts,
    performance_parameters,
    read_tts,
    register_result,
    require_tts,
    require_tts_voice,
)
from arclight.application.audio_versions import require_audio_file, require_audio_version
from arclight.application.audio_voice_profiles import (
    profile_for_line,
    resolve_selection,
    script_content,
)
from arclight.application.audio_voices import voice_spec
from arclight.application.audit import audit
from arclight.application.media_services import (
    credential_for_selection,
    quote_selection,
    select_for_spec,
)
from arclight.application.music_assets import music_resource_facts, require_music_version
from arclight.application.production_versions import bind, find_binding
from arclight.application.render_inputs import font_facts
from arclight.application.short_drama_delivery import approved_audio, require_run_authorization
from arclight.application.timelines import approved_board
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import next_status
from arclight.domain.audio_production import AudioLine, AudioSnapshotCreate
from arclight.domain.audio_tts import AudioTTSCreate, AudioTTSRegister
from arclight.domain.character_names import canonical_name
from arclight.domain.short_drama import BlockedVoice, NeededVoice
from arclight.domain.timeline_audio import ClipAudio
from arclight.domain.timelines import ClipSnapshot
from arclight.domain.versions import digest
from arclight.persistence.audio_production import AudioSnapshot
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    GenerationMediaArtifact,
    Script,
    Shot,
    VisualAsset,
    WorkflowRun,
)

SOUND_PLAN = "SHORT_DRAMA_SOUND_PLAN"
DIALOGUE_PLAN = "SHORT_DRAMA_DIALOGUE"

VOICE_BLOCK_REASONS = {
    "AUDIO_FILE_INVALID": "已批准录音的文件回执或使用授权不可用。",
    "AUDIO_VOICE_SCENE_UNMAPPED": "现有声音档案尚未绑定本版剧本的场景时点。",
    "AUDIO_VOICE_LEDGER_NOT_APPROVED": "现有声音档案需要已批准的故事账本。",
    "AUDIO_VOICE_PROFILE_CONFLICT": "角色声音档案与当前台词的音色不一致。",
    "AUDIO_PERFORMANCE_UNSUPPORTED": "已配置的音色不支持当前台词的表演要求。",
    "BUDGET_QUOTE_UNAVAILABLE": "已配置的语音服务缺少可信报价。",
    "BUDGET_QUOTE_CHANGED": "语音服务报价已变化，请重新保存音色。",
    "SERVICE_KEY_UNAVAILABLE": "已保存音色的语音服务凭据不可用。",
    "FORBIDDEN": "已保存音色的服务账号已失去本项目的写入权限或已停用。",
}
REPLACEABLE_VOICE_ERRORS = frozenset(
    {"BUDGET_QUOTE_CHANGED", "SERVICE_KEY_UNAVAILABLE", "FORBIDDEN"}
)


@dataclass(frozen=True)
class ShortDramaSound:
    voice_asset_versions: dict[str, UUID]
    sound_clips: tuple[ClipSnapshot, ...]


class SoundPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    storyboard_id: UUID
    audio_snapshot_id: UUID
    voice_asset_versions: dict[str, UUID]
    voice_jobs: dict[str, UUID]
    sound_clips: tuple[ClipSnapshot, ...]


class DialogueVoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    voice_version_id: UUID
    voice_profile_version_id: UUID | None


class DialoguePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_snapshot_id: UUID
    voice_asset_versions: dict[str, UUID]
    voices: dict[str, DialogueVoice]


def voice_request(
    db: Session, project_id: UUID, snapshot: AudioSnapshot, content: ScriptContent, line: AudioLine
) -> tuple[UUID, UUID | None]:
    character_id, profile = profile_for_line(db, project_id, content, line)
    if profile is not None:
        selection = resolve_selection(db, project_id, snapshot, content, line, profile)
        return selection.voice_version_id, profile.id
    voice = db.scalar(
        select(AudioVoiceVersion)
        .where(
            AudioVoiceVersion.project_id == project_id,
            AudioVoiceVersion.speaker == line.speaker,
            AudioVoiceVersion.character_asset_id == character_id,
        )
        .order_by(AudioVoiceVersion.version_no.desc())
        .limit(1)
    )
    if voice is None:
        raise DomainError(
            "SHORT_DRAMA_VOICE_REQUIRED",
            f"角色「{line.speaker}」缺少可用的项目音色或已批准配音",
            409,
        )
    return voice.id, None


def dialogue_voice(
    db: Session,
    settings: Settings,
    project_id: UUID,
    actor: str,
    snapshot: AudioSnapshot,
    content: ScriptContent,
    line: AudioLine,
) -> DialogueVoice:
    voice_id, profile_id = voice_request(db, project_id, snapshot, content, line)
    voice, selection = require_tts_voice(
        db, project_id, snapshot, line, actor, voice_id, profile_id
    )
    spec = voice_spec(voice, line.text, performance_parameters(voice, selection, {}))
    service = select_for_spec(db, actor, spec, "AUDIO", voice.service_snapshot_json)
    if service is None or quote_selection(db, service, spec) is None:
        raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "语音服务缺少可信报价", 409)
    try:
        credential_for_selection(db, settings, project_id, service)
    except DomainError as error:
        if error.code != "NOT_FOUND":
            raise
        raise DomainError("FORBIDDEN", "原媒体凭证所属账号已失去项目访问权限", 403) from error
    return DialogueVoice(voice_version_id=voice_id, voice_profile_version_id=profile_id)


def inspect_dialogue(
    db: Session, settings: Settings, project_id: UUID, actor: str, script: Script
) -> tuple[list[NeededVoice], list[BlockedVoice]]:
    content = read_script_content(script.content_json)
    lines = script_audio_lines(content)
    if len(lines) > 128:
        raise DomainError("SHORT_DRAMA_AUDIO_LIMIT", "单集自动字幕最多支持 128 句台词", 422)
    snapshot = AudioSnapshot(episode_id=script.episode_id, script_version_id=script.id)
    needed: dict[str, NeededVoice] = {}
    blocked: dict[str, BlockedVoice] = {}
    for line in lines:
        try:
            recording = approved_audio(db, None, snapshot, line.line_id, None)
            if recording is None:
                dialogue_voice(db, settings, project_id, actor, snapshot, content, line)
            else:
                require_audio_file(db, project_id, *recording)
        except DomainError as error:
            if error.status_code >= 500:
                raise
            if error.code == "SHORT_DRAMA_VOICE_REQUIRED" or error.code in REPLACEABLE_VOICE_ERRORS:
                character_id, profile = profile_for_line(db, project_id, content, line)
                if profile is None:
                    needed[line.speaker] = NeededVoice(
                        speaker=line.speaker, character_asset_id=character_id
                    )
            if error.code != "SHORT_DRAMA_VOICE_REQUIRED":
                blocked.setdefault(
                    line.speaker,
                    BlockedVoice(
                        speaker=line.speaker,
                        reason=VOICE_BLOCK_REASONS.get(
                            error.code, "当前台词的录音或音色配置不可用。"
                        ),
                    ),
                )
    return list(needed.values()), list(blocked.values())


def preflight_dialogue(
    db: Session, settings: Settings, run: WorkflowRun, project_id: UUID, actor: str, script_id: UUID
) -> None:
    snapshot = short_drama_audio_snapshot(db, run, actor, script_id)
    if snapshot.line_count > 128:
        raise DomainError("SHORT_DRAMA_AUDIO_LIMIT", "单集自动字幕最多支持 128 句台词", 422)
    content = script_content(db, snapshot)
    result = DialoguePlan(audio_snapshot_id=snapshot.id, voice_asset_versions={}, voices={})
    for data in snapshot.lines_json:
        line = AudioLine.model_validate(data)
        recording = approved_audio(db, run.id, snapshot, line.line_id, None)
        if recording is None:
            result.voices[line.line_id] = dialogue_voice(
                db, settings, project_id, actor, snapshot, content, line
            )
        else:
            _, version = recording
            asset = db.get(VisualAsset, version.asset_id)
            assert asset is not None
            require_audio_version(db, settings, project_id, asset, version, actor)
            result.voice_asset_versions[line.line_id] = version.id
    bind(db, project_id, DIALOGUE_PLAN, run.id, result.model_dump(mode="json"))


def short_drama_audio_snapshot(
    db: Session, run: WorkflowRun, actor: str, script_id: UUID
) -> AudioSnapshot:
    return capture_snapshot(
        db,
        run.episode_id,
        actor,
        AudioSnapshotCreate(
            creation_key=uuid5(run.id, "short-drama:audio"), script_version_id=script_id
        ),
    )


def read_dialogue_plan(
    db: Session, project_id: UUID, run_id: UUID, snapshot: AudioSnapshot
) -> DialoguePlan:
    binding = find_binding(db, project_id, DIALOGUE_PLAN, run_id)
    invalid = DomainError("SHORT_DRAMA_AUDIO_SCOPE", "缺少本次确认的精确台词来源", 409)
    if binding is None:
        raise invalid
    try:
        plan = DialoguePlan.model_validate(binding.snapshot_json)
    except ValidationError as exc:
        raise invalid from exc
    if (
        plan.audio_snapshot_id != snapshot.id
        or plan.voice_asset_versions.keys() & plan.voices.keys()
        or plan.voice_asset_versions.keys() | plan.voices.keys()
        != {line["line_id"] for line in snapshot.lines_json}
    ):
        raise invalid
    return plan


def available_sound_effects(
    db: Session,
    project_id: UUID,
    *,
    names: set[str] | None = None,
    limit: int | None = None,
) -> dict[str, tuple[VisualAsset, AssetVersion]]:
    sources: dict[str, list[tuple[VisualAsset, AssetVersion]]] = {}
    for asset, version in db.execute(
        select(VisualAsset, AssetVersion)
        .join(AssetVersion, AssetVersion.id == VisualAsset.current_version_id)
        .where(
            VisualAsset.project_id == project_id,
            VisualAsset.asset_type == "MUSIC",
            VisualAsset.status.not_in(("ARCHIVED", "STALE", "REJECTED")),
            AssetVersion.status.in_(("APPROVED", "LOCKED")),
            AssetVersion.stale.is_(False),
        )
    ):
        name = canonical_name(asset.name)
        if names is None or name in names:
            sources.setdefault(name, []).append((asset, version))
    available = {}
    for name, matches in sorted(sources.items()):
        if len(matches) != 1:
            continue
        asset, version = matches[0]
        try:
            resource = music_resource_facts(db, project_id, asset, version)
        except DomainError as error:
            if error.status_code >= 500:
                raise
            continue
        if resource.purpose_role != "BACKGROUND":
            available[name] = (asset, version)
            if limit is not None and len(available) >= limit:
                break
    return available


def select_sound_clips(
    db: Session,
    settings: Settings,
    run_id: UUID,
    project_id: UUID,
    actor: str,
    shots: list[Shot],
) -> tuple[ClipSnapshot, ...]:
    casts = [(shot, read_shot_content(shot.content_json).sound_effects) for shot in shots]
    names = {canonical_name(cue) for _, cues in casts for cue in cues}
    if not names:
        return ()
    sources = available_sound_effects(db, project_id, names=names)
    clips: list[ClipSnapshot] = []
    cursor = 0
    for shot, cues in casts:
        for cue in dict.fromkeys(canonical_name(cue) for cue in cues):
            match = sources.get(cue)
            if match is None:
                raise DomainError(
                    "SHORT_DRAMA_SFX_REQUIRED",
                    f"镜头 {shot.shot_no} 的音效「{cue}」需要唯一的已选用授权素材",
                    409,
                )
            asset, version = match
            resource = require_music_version(db, settings, project_id, asset, version, actor)
            if len(clips) == 256:
                raise DomainError(
                    "SHORT_DRAMA_AUDIO_LIMIT", "单集自动制作最多支持 256 个音效片段", 422
                )
            duration = min(shot.duration_ms, resource.validation.duration_ms)
            clips.append(
                ClipSnapshot(
                    id=uuid5(run_id, f"short-drama:sfx:{shot.id}:{cue}"),
                    ordinal=len(clips),
                    source_asset_version_id=version.id,
                    source_sha256=version.checksum,
                    start_ms=cursor,
                    duration_ms=duration,
                    shot_id=shot.id,
                    audio=ClipAudio(
                        purpose_role=resource.purpose_role,
                        gain_milli=250,
                        fade_out_ms=min(100, duration),
                    ),
                )
            )
        cursor += shot.duration_ms
    return tuple(clips)


def prepare_sound_plan(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    project_id: UUID,
    actor: str,
    snapshot: AudioSnapshot,
    storyboard_id: UUID,
    dialogue: DialoguePlan,
    sound_clips: tuple[ClipSnapshot, ...],
    rights_confirmed: bool,
) -> SoundPlan:
    if dialogue.voices and not rights_confirmed:
        raise DomainError(
            "SHORT_DRAMA_GENERATED_RIGHTS_REQUIRED", "自动配音需要所有者确认生成素材的使用权", 409
        )
    jobs = {}
    for line_id, voice in dialogue.voices.items():
        result, _ = create_tts(
            db,
            snapshot.id,
            line_id,
            actor,
            AudioTTSCreate(
                creation_key=uuid5(run.id, f"short-drama:tts:{line_id}"),
                voice_version_id=voice.voice_version_id,
                voice_profile_version_id=voice.voice_profile_version_id,
            ),
            execution_policy=settings.media_execution_policy,
            short_drama_run_id=run.id,
        )
        jobs[line_id] = result.job_id
    plan = SoundPlan(
        storyboard_id=storyboard_id,
        audio_snapshot_id=snapshot.id,
        voice_asset_versions=dialogue.voice_asset_versions,
        voice_jobs=jobs,
        sound_clips=sound_clips,
    )
    bind(db, project_id, SOUND_PLAN, run.id, plan.model_dump(mode="json"))
    return plan


def accept_tts(
    db: Session,
    settings: Settings,
    run_id: UUID,
    snapshot: AudioSnapshot,
    line_id: str,
    job_id: UUID,
    actor: str,
    voice: DialogueVoice,
) -> UUID | None:
    request = require_tts(db, snapshot.id, line_id, job_id, actor, write=True)
    if (
        request.creation_key != uuid5(run_id, f"short-drama:tts:{line_id}")
        or request.voice_version_id != voice.voice_version_id
    ):
        raise DomainError("SHORT_DRAMA_AUDIO_SCOPE", "配音任务不属于本次自动制作", 409)
    state = read_tts(db, request)
    if state.status in {"FAILED", "CANCELLED"}:
        raise DomainError(
            state.error_code or "SHORT_DRAMA_TTS_FAILED", "自动配音任务未能生成可交付结果", 409
        )
    if state.status == "MANUAL_REVIEW":
        raise DomainError("SHORT_DRAMA_TTS_UNCERTAIN", "自动配音结果尚无可核验的供应商回执", 409)
    if state.status != "SUCCEEDED":
        return None
    artifact = db.scalar(
        select(GenerationMediaArtifact)
        .join(GenerationAttempt, GenerationAttempt.id == GenerationMediaArtifact.attempt_id)
        .where(GenerationMediaArtifact.job_id == job_id, GenerationAttempt.status == "SUCCEEDED")
        .limit(1)
    )
    if artifact is None:
        return None
    if artifact.error_code or artifact.file_id is None:
        raise DomainError(
            artifact.error_code or "SHORT_DRAMA_TTS_FAILED", "配音结果未能通过素材归档检查", 409
        )
    asset = db.get(VisualAsset, request.target_asset_id, populate_existing=True)
    assert asset is not None
    result, _ = register_result(
        db,
        settings,
        request,
        actor,
        AudioTTSRegister(
            expected_revision=asset.revision,
            source_note=f"根据已确认剧本台词生成。配音任务 {job_id}",
            rights_note=f"项目所有者在自动制片授权 {run_id} 中确认生成素材的使用权。",
            rights_confirmed=True,
        ),
    )
    version = db.get(AssetVersion, result.binding.asset_version_id, with_for_update=True)
    assert version is not None
    if version.status not in {"APPROVED", "LOCKED"}:
        require_audio_version(db, settings, request.project_id, asset, version, actor)
        previous = version.status
        if previous == "DRAFT":
            version.status = next_status(previous, "READY_FOR_REVIEW", version.archived_from_status)
            version.revision += 1
        version.status = next_status(version.status, "APPROVED", version.archived_from_status)
        version.revision += 1
        audit(
            db,
            request.project_id,
            actor,
            "short_drama.audio_approved",
            version.id,
            {"run_id": str(run_id), "job_id": str(job_id), "from": previous, "to": version.status},
        )
        db.flush()
    return version.id


def advance_short_drama_audio(
    db: Session, settings: Settings, run: WorkflowRun, storyboard_id: UUID
) -> ShortDramaSound | None:
    """Reuse exact media or advance durable TTS jobs in the caller's transaction."""
    episode, actor, spec = require_run_authorization(db, run)
    script = approved_script(db, episode, UUID(spec["script_version_id"]), actor)
    if digest(script.content_json) != spec.get("script_sha256"):
        raise DomainError("SHORT_DRAMA_SCRIPT_CHANGED", "自动制作授权的剧本摘要已变化", 409)
    board, shots = approved_board(db, episode, storyboard_id)
    if board.script_version_id != script.id:
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "分镜须来自本次确认的精确剧本", 409)
    if len(shots) > 63:
        raise DomainError("TIMELINE_SHOTS_INCOMPLETE", "单集自动制作最多支持 63 个镜头", 422)
    snapshot = short_drama_audio_snapshot(db, run, actor, script.id)
    lines = [AudioLine.model_validate(line) for line in snapshot.lines_json]
    if len(lines) > 128:
        raise DomainError("SHORT_DRAMA_AUDIO_LIMIT", "单集自动字幕最多支持 128 句台词", 422)
    font_id = UUID(spec["font_file_id"]) if spec.get("font_file_id") else None
    font_facts(db, episode.project_id, font_id, required=bool(lines))
    shot_lines = [(shot, read_shot_content(shot.content_json).dialogue_line_ids) for shot in shots]
    if Counter(line for _, identities in shot_lines for line in identities) != Counter(
        line.line_id for line in lines
    ):
        raise DomainError("SHORT_DRAMA_AUDIO_COVERAGE", "分镜须为每句剧本台词安排唯一镜头", 409)

    binding = find_binding(db, episode.project_id, SOUND_PLAN, run.id)
    dialogue = read_dialogue_plan(db, episode.project_id, run.id, snapshot)
    rights_confirmed = spec.get("generated_media_rights_confirmed") is True
    if binding is None:
        clips = select_sound_clips(db, settings, run.id, episode.project_id, actor, shots)
        plan = prepare_sound_plan(
            db,
            settings,
            run,
            episode.project_id,
            actor,
            snapshot,
            board.id,
            dialogue,
            clips,
            rights_confirmed,
        )
    else:
        try:
            plan = SoundPlan.model_validate(binding.snapshot_json)
        except ValidationError as exc:
            raise DomainError("SHORT_DRAMA_AUDIO_SCOPE", "已冻结声音制作安排格式无效", 409) from exc
    if (
        plan.storyboard_id != board.id
        or plan.audio_snapshot_id != snapshot.id
        or plan.voice_asset_versions != dialogue.voice_asset_versions
        or plan.voice_jobs.keys() != dialogue.voices.keys()
        or (plan.voice_jobs and not rights_confirmed)
    ):
        raise DomainError("SHORT_DRAMA_AUDIO_SCOPE", "已冻结声音制作安排与当前授权不一致", 409)

    exact = dict(plan.voice_asset_versions)
    for line_id, job_id in plan.voice_jobs.items():
        identity = accept_tts(
            db, settings, run.id, snapshot, line_id, job_id, actor, dialogue.voices[line_id]
        )
        if identity is not None:
            exact[line_id] = identity
    if len(exact) != len(lines):
        return None
    for shot, identities in shot_lines:
        duration_ms = 0
        for line_id in identities:
            recording = approved_audio(db, run.id, snapshot, line_id, exact[line_id])
            if recording is None:
                raise DomainError("SHORT_DRAMA_AUDIO_REQUIRED", "选定的精确台词配音已不可用", 409)
            line_binding, version = recording
            asset = db.get(VisualAsset, version.asset_id)
            assert asset is not None
            require_audio_version(db, settings, episode.project_id, asset, version, actor)
            duration_ms += line_binding.duration_ms
        if duration_ms > shot.duration_ms:
            raise DomainError(
                "SHORT_DRAMA_AUDIO_DURATION", f"镜头 {shot.shot_no} 的配音超过画面时长", 409
            )
    return ShortDramaSound(exact, plan.sound_clips)
