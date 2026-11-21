"""Versioned character voices and their precise impact on line production."""

from collections import defaultdict
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    Scene,
    SceneV3,
    ScriptContent,
    ScriptContentV3,
    read_script_content,
)
from arclight.application.audio_voices import read_voice, require_voice
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import exact_version, lock_dependency_graph
from arclight.application.invalidation import invalidate
from arclight.application.story_ledger import require_version, timestamp, version_approval
from arclight.core.errors import DomainError
from arclight.domain.audio_production import AudioLine
from arclight.domain.audio_voice_profiles import (
    LinePerformance,
    LineVoiceProfileRead,
    VoiceProfileCreate,
    VoiceProfilePlan,
    VoiceProfileRead,
    VoiceSelection,
)
from arclight.domain.story_bible import canonical_name
from arclight.domain.story_ledger import LedgerSceneTime, StoryLedgerContent, read_ledger_content
from arclight.domain.versions import digest
from arclight.persistence.audio_production import AudioSnapshot
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Script,
    StoryOutline,
    VisualAsset,
)
from arclight.providers.media_capabilities import parameter_ranges, resolve_parameters

PROFILE_KIND = "AUDIO_VOICE_PROFILE"


def profile_id(character_id: UUID) -> UUID:
    return uuid5(character_id, "audio-voice-profile")


def character_asset(db: Session, project_id: UUID, character_id: UUID) -> VisualAsset:
    row = db.get(VisualAsset, character_id, populate_existing=True)
    if row is None or row.project_id != project_id or row.asset_type != "CHARACTER":
        raise DomainError("NOT_FOUND", "角色资产不存在", 404)
    return row


def read_profile(version: AssetVersion, character_id: UUID) -> VoiceProfileRead:
    data = version.metadata_json
    frozen = {key: data.get(key) for key in ("plan", "ledger_approval_id")}
    if version.asset_id != profile_id(character_id) or version.checksum != digest(frozen):
        raise DomainError("AUDIO_VOICE_PROFILE_INVALID", "声音档案版本与保存凭证不符", 409)
    return VoiceProfileRead(
        id=version.id,
        character_asset_id=character_id,
        version_no=version.version_no,
        plan=VoiceProfilePlan.model_validate(data["plan"]),
        ledger_approval_id=UUID(data["ledger_approval_id"]),
        created_by=version.created_by,
        created_at=timestamp(version.created_at),
        invalidation_id=data.get("invalidation_id"),
    )


def get_profile(
    db: Session, project_id: UUID, character_id: UUID, version_id: UUID | None = None
) -> VoiceProfileRead | None:
    asset = db.get(VisualAsset, profile_id(character_id), populate_existing=True)
    identity = version_id or (asset.current_version_id if asset else None)
    if identity is None:
        return None
    version, owner = exact_version(db, project_id, identity)
    if owner.id != profile_id(character_id) or owner.asset_type != PROFILE_KIND:
        raise DomainError("NOT_FOUND", "角色声音档案版本不存在", 404)
    return read_profile(version, character_id)


def approved_voice_ledger(
    db: Session, project_id: UUID, ledger_id: UUID
) -> tuple[StoryLedgerContent, UUID]:
    ledger = require_version(db, project_id, ledger_id)
    approval = version_approval(db, ledger)
    if approval is None:
        raise DomainError("AUDIO_VOICE_LEDGER_NOT_APPROVED", "声音档案需要已批准的故事账本", 409)
    return read_ledger_content(ledger.content_json), approval.id


def voice_selector(snapshot: AudioSnapshot, line_id: str) -> str:
    return f"voice:{snapshot.episode_id}:{snapshot.script_version_id}:{line_id}"


def manifest_voice(context: dict) -> VoiceSelection | None:
    if "voice_selection" not in context:
        return None
    try:
        return VoiceSelection.model_validate(context["voice_selection"])
    except ValidationError as error:
        raise DomainError("AUDIO_PROVENANCE_REQUIRED", "声音档案输入凭证无效", 409) from error


def profile_changes(
    db: Session,
    project_id: UUID,
    character_id: UUID,
    plan: VoiceProfilePlan,
    mappings: list[LedgerSceneTime],
) -> list[dict]:
    scenes: dict[tuple[UUID, UUID], list[LedgerSceneTime]] = defaultdict(list)
    for mapping in mappings:
        scenes[(mapping.episode_id, mapping.scene_id)].append(mapping)
    selectors: dict[UUID, set[str]] = defaultdict(set)
    requests = db.execute(
        select(AudioTTSRequest, DependencyManifest, AudioSnapshot)
        .select_from(AudioTTSRequest)
        .join(AudioSnapshot, AudioSnapshot.id == AudioTTSRequest.snapshot_id)
        .join(AudioVoiceVersion, AudioVoiceVersion.id == AudioTTSRequest.voice_version_id)
        .join(DependencyManifest, DependencyManifest.id == AudioTTSRequest.dependency_manifest_id)
        .where(
            AudioTTSRequest.project_id == project_id,
            AudioVoiceVersion.character_asset_id == character_id,
        )
    )
    for request, manifest, snapshot in requests:
        selection = manifest_voice(manifest.snapshot_json["context"])
        if selection is None:
            continue
        candidates = scenes[(snapshot.episode_id, selection.scene_id)]
        exact = [
            item for item in candidates if item.script_version_id == snapshot.script_version_id
        ]
        # A dialogue-only script revision may carry the same scene into a new ledger.
        # An unmapped or ambiguous scene never substitutes a different story moment.
        script = db.get(Script, snapshot.script_version_id)
        pool = exact or candidates
        if script is not None and script.content_json.get("schema_version") == "3.0":
            pool = [item for item in pool if item.story_time == selection.story_time]
        if (
            len(pool) != 1
            or pool[0].segment_no != selection.segment_no
            or pool[0].story_time != selection.story_time
            or selection.voice_version_id != plan.voice_at(selection.story_time)
        ):
            selectors[selection.profile_version_id].add(voice_selector(snapshot, request.line_id))
    changes = []
    for identity, scope in sorted(selectors.items(), key=lambda item: str(item[0])):
        version, asset = exact_version(db, project_id, identity)
        if asset.id != profile_id(character_id) or asset.asset_type != PROFILE_KIND:
            raise DomainError("AUDIO_PROVENANCE_REQUIRED", "声音档案依赖来源不符", 409)
        changes.append(
            {
                "source_asset_version_id": str(identity),
                "expected_revision": version.revision,
                "change_kind": "CONTENT",
                "selectors": sorted(scope),
            }
        )
    return changes


def save_profile(
    db: Session, project_id: UUID, character_id: UUID, actor: str, payload: VoiceProfileCreate
) -> tuple[VoiceProfileRead, bool]:
    require_project(db, project_id, actor, write=True)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    character = character_asset(db, project_id, character_id)
    asset_id = profile_id(character_id)
    request_digest = digest(
        {
            "actor": actor,
            "request": payload.model_dump(mode="json", exclude={"expected_version_no"}),
        }
    )
    previous = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset_id, AssetVersion.creation_key == payload.creation_key
        )
    )
    if previous is not None:
        if previous.metadata_json.get("request_sha256") != request_digest:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一保存键不能修改声音档案内容", 409)
        return read_profile(previous, character_id), False
    if character.status in {"ARCHIVED", "STALE", "REJECTED"}:
        raise DomainError("ASSET_NOT_USABLE", "该角色当前不能设置声音档案", 409)
    current = get_profile(db, project_id, character_id)
    latest = current.version_no if current else 0
    if latest != payload.expected_version_no:
        raise DomainError("AUDIO_VOICE_PROFILE_CONFLICT", "声音档案已更新，请刷新后再保存", 409)
    plan = VoiceProfilePlan.model_validate(
        payload.model_dump(exclude={"creation_key", "expected_version_no"})
    )
    content, approval_id = approved_voice_ledger(db, project_id, plan.ledger_version_id)
    for voice_id in {
        plan.default_voice_version_id,
        *(change.voice_version_id for change in plan.changes),
    }:
        voice = require_voice(db, project_id, voice_id, actor, write=True)
        if voice.character_asset_id != character_id:
            raise DomainError("AUDIO_VOICE_CHARACTER_MISMATCH", "该音色未绑定当前角色资产", 422)
    asset = db.get(VisualAsset, asset_id)
    if asset is None:
        asset = VisualAsset(
            id=asset_id,
            project_id=project_id,
            asset_type=PROFILE_KIND,
            name=f"{character.name} · 声音档案"[:160],
            status="APPROVED",
            created_by=actor,
            metadata_json={"character_asset_id": str(character.id)},
        )
        db.add(asset)
        db.flush()
    identity = uuid4()
    changes = (
        profile_changes(db, project_id, character_id, plan, content.scene_times) if current else []
    )
    event_id = None
    if changes:
        event, _ = invalidate(db, project_id, actor, identity, changes, "已保存新的跨集声音档案")
        event_id = str(event.id)
    frozen = {"plan": plan.model_dump(mode="json"), "ledger_approval_id": str(approval_id)}
    version = AssetVersion(
        id=identity,
        asset_id=asset_id,
        version_no=latest + 1,
        object_key=f"database:audio_voice_profile:{identity}",
        checksum=digest(frozen),
        created_by=actor,
        status="APPROVED",
        creation_key=payload.creation_key,
        metadata_json=frozen
        | {
            "request_sha256": request_digest,
            "invalidation_id": event_id,
            "previous_version_id": str(current.id) if current else None,
        },
    )
    db.add(version)
    db.flush()
    asset.current_version_id = version.id
    asset.revision += 1
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "audio.voice_profile.saved",
        version.id,
        {
            "character_asset_id": str(character_id),
            "version_no": version.version_no,
            "ledger_approval_id": str(approval_id),
            "invalidation_id": event_id,
        },
    )
    return read_profile(version, character_id), True


def character_for(content: ScriptContent, speaker: str) -> UUID | None:
    return next(
        (
            UUID(item.asset.asset_id)
            for item in content.character_assets
            if item.character == speaker
        ),
        None,
    )


def scene_for(content: ScriptContent, line: AudioLine) -> Scene:
    for scene in content.scenes:
        if scene.scene_id == line.scene_id and any(
            item.line_id == line.line_id for item in scene.dialogue
        ):
            return scene
    raise DomainError("AUDIO_PROVENANCE_REQUIRED", "剧本与台词快照的场景不符", 409)


def performance_for(
    db: Session, content: ScriptContent, scene: Scene, line: AudioLine
) -> LinePerformance:
    beat = scene.dramatic_beat if isinstance(scene, SceneV3) else None
    objective = None
    if beat is not None and isinstance(content, ScriptContentV3):
        context = content.narrative_context
        plan = db.get(StoryOutline, context.series_plan_version_id)
        if (
            plan is None
            or plan.project_id != context.project_id
            or digest(plan.content_json) != context.plan_sha256
            or digest(plan.source_snapshot) != context.plan_source.get("source_snapshot_sha256")
        ):
            raise DomainError("AUDIO_PROVENANCE_REQUIRED", "场景表演缺少匹配的全剧人物来源", 409)
        character = next(
            (
                row["id"]
                for row in plan.source_snapshot.get("series_plan_references", {}).get(
                    "characters", []
                )
                if canonical_name(row["name"]) == canonical_name(line.speaker)
            ),
            None,
        )
        objective = next(
            (item for item in beat.character_objectives if str(item.character_id) == character),
            None,
        )
    return LinePerformance(
        scene_title=scene.title,
        sequence_no=line.sequence_no,
        emotion=line.emotion,
        purpose=objective.purpose if objective else beat.conflict if beat else "",
        emotion_start=objective.emotion_start if objective else "",
        emotion_end=objective.emotion_end if objective else "",
        interactions=[
            item.action
            for item in beat.dialogue_interactions
            if UUID(line.line_id) in item.line_ids
        ]
        if beat
        else [],
    )


def resolve_selection(
    db: Session,
    project_id: UUID,
    snapshot: AudioSnapshot,
    content: ScriptContent,
    line: AudioLine,
    profile: VoiceProfileRead,
) -> VoiceSelection:
    ledger, approval_id = approved_voice_ledger(db, project_id, profile.plan.ledger_version_id)
    scene = scene_for(content, line)
    beat = scene.dramatic_beat if isinstance(scene, SceneV3) else None
    scene_id = UUID(scene.scene_id)
    matches = [
        item
        for item in ledger.scene_times
        if (
            item.episode_id == snapshot.episode_id
            and item.script_version_id == snapshot.script_version_id
            and item.scene_id == scene_id
            and (beat is None or item.story_time == beat.story_time)
        )
    ]
    if len(matches) != 1 or approval_id != profile.ledger_approval_id:
        raise DomainError(
            "AUDIO_VOICE_SCENE_UNMAPPED", "声音档案的已批准账本须包含本版剧本的唯一场景时点", 409
        )
    mapping = matches[0]
    change = profile.plan.change_at(mapping.story_time)
    return VoiceSelection(
        profile_version_id=profile.id,
        character_asset_id=profile.character_asset_id,
        voice_version_id=profile.plan.voice_at(mapping.story_time),
        ledger_version_id=profile.plan.ledger_version_id,
        ledger_approval_id=approval_id,
        episode_id=snapshot.episode_id,
        scene_id=scene_id,
        mapping_id=mapping.id,
        segment_no=mapping.segment_no,
        story_time=mapping.story_time,
        change_reason=change.reason if change else None,
        performance=performance_for(db, content, scene, line),
    )


def script_content(db: Session, snapshot: AudioSnapshot) -> ScriptContent:
    script = db.get(Script, snapshot.script_version_id)
    if script is None or script.episode_id != snapshot.episode_id:
        raise DomainError("AUDIO_PROVENANCE_REQUIRED", "台词快照缺少精确剧本", 409)
    return read_script_content(script.content_json)


def profile_for_line(
    db: Session, project_id: UUID, content: ScriptContent, line: AudioLine
) -> tuple[UUID | None, VoiceProfileRead | None]:
    identity = character_for(content, line.speaker)
    return identity, get_profile(db, project_id, identity) if identity is not None else None


def line_profile_read(
    db: Session, project_id: UUID, snapshot: AudioSnapshot, line: AudioLine, actor: str
) -> LineVoiceProfileRead:
    content = script_content(db, snapshot)
    identity, profile = profile_for_line(db, project_id, content, line)
    if profile is None:
        return LineVoiceProfileRead(character_asset_id=identity)
    try:
        selection = resolve_selection(db, project_id, snapshot, content, line, profile)
    except DomainError as error:
        if error.code != "AUDIO_VOICE_SCENE_UNMAPPED":
            raise
        return LineVoiceProfileRead(
            character_asset_id=identity, profile=profile, unavailable_reason=error.message
        )
    voice = read_voice(require_voice(db, project_id, selection.voice_version_id, actor))
    if voice.speaker != line.speaker:
        return LineVoiceProfileRead(
            character_asset_id=identity,
            profile=profile,
            unavailable_reason="本句说话者与已保存音色的台词名称不一致，请核对剧本角色。",
        )
    supported = {"speed", "instructions"} & resolve_parameters(voice.provider, voice.model)
    return LineVoiceProfileRead(
        character_asset_id=identity,
        profile=profile,
        voice=voice,
        selection=selection,
        supported_parameters=sorted(supported),
        parameter_ranges={
            name: limits
            for name, limits in parameter_ranges(voice.provider, voice.model).items()
            if name in supported
        },
    )
