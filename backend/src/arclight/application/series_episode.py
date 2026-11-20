"""Small, exact-version episode summary for the series section and stage navigator."""

from uuid import UUID, uuid5

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    ScriptContentV3,
    ShotContentV3,
    read_script_content,
    read_shot_content,
)
from arclight.application.audio_voice_profiles import manifest_voice
from arclight.application.content_dependencies import reference_ids
from arclight.application.production_state import production_facts
from arclight.core.errors import DomainError
from arclight.domain.production_status import episode_state
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Script,
    Shot,
    Storyboard,
    StoryOutline,
    VisualAsset,
)
from arclight.persistence.render_manifests import RenderArtifact

AUDIO_EVIDENCE_LIMIT = 20


class VersionLink(BaseModel):
    id: UUID
    asset_id: UUID
    name: str
    version_no: int
    stale: bool


class ReviewedVideo(BaseModel):
    version_id: UUID
    shot_no: int
    temporal_review_id: UUID
    lip_sync_reviewed: bool


class UsedVoice(BaseModel):
    version_id: UUID
    version_no: int
    speaker: str
    profile_version_id: UUID | None


class BoundAudio(BaseModel):
    version_id: UUID
    line_id: str
    version_no: int
    status: str
    stale: bool
    voice: UsedVoice | None


class SeriesEpisodeEvidence(BaseModel):
    project_id: UUID
    episode_id: UUID
    phase: str
    issues: list[str]
    script_version_id: UUID | None = None
    storyboard_version_id: UUID | None = None
    outline_version_id: UUID | None = None
    outline_episode_id: UUID | None = None
    ledger_version_id: UUID | None = None
    target_story_time: int | None = None
    performance_shots: int = 0
    dynamic_shots: int = 0
    reviewed_dynamic_shots: int = 0
    visible_dialogue_shots: int = 0
    required_lip_sync_shots: int = 0
    reviewed_videos: list[ReviewedVideo] = Field(default_factory=list)
    audio_snapshot_id: UUID | None = None
    audio_line_count: int | None = None
    bound_audio_total: int = 0
    bound_audio: list[BoundAudio] = Field(default_factory=list)
    delivery_id: UUID | None = None
    delivery_reviewed: bool = False
    shared_versions: list[VersionLink] = Field(default_factory=list)


def _bound_audio(db: Session, project_id: UUID, script: Script) -> tuple[int, list[BoundAudio]]:
    """Read the latest bound recording per line, not a mutable voice preference."""
    latest = (
        select(
            AudioLineBinding.id.label("binding_id"),
            func.row_number()
            .over(
                partition_by=AudioLineBinding.line_id,
                order_by=(AudioLineBinding.created_at.desc(), AudioLineBinding.id.desc()),
            )
            .label("rank"),
        )
        .join(AudioSnapshot, AudioSnapshot.id == AudioLineBinding.snapshot_id)
        .join(AssetVersion, AssetVersion.id == AudioLineBinding.asset_version_id)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(
            AudioSnapshot.episode_id == script.episode_id,
            AudioSnapshot.script_version_id == script.id,
            VisualAsset.project_id == project_id,
            VisualAsset.asset_type == "AUDIO",
        )
        .subquery()
    )
    rows = db.execute(
        select(AudioLineBinding, AssetVersion, func.count().over())
        .join(latest, latest.c.binding_id == AudioLineBinding.id)
        .join(AssetVersion, AssetVersion.id == AudioLineBinding.asset_version_id)
        .where(latest.c.rank == 1)
        .order_by(AudioLineBinding.line_id)
        .limit(AUDIO_EVIDENCE_LIMIT)
    ).all()
    if not rows:
        return 0, []
    voices = {
        uuid5(request.job_id, "audio-tts-result"): (request, voice, manifest)
        for request, voice, manifest in db.execute(
            select(AudioTTSRequest, AudioVoiceVersion, DependencyManifest)
            .join(AudioVoiceVersion, AudioVoiceVersion.id == AudioTTSRequest.voice_version_id)
            .join(
                DependencyManifest, DependencyManifest.id == AudioTTSRequest.dependency_manifest_id
            )
            .where(
                AudioTTSRequest.project_id == project_id,
                AudioVoiceVersion.project_id == project_id,
                DependencyManifest.project_id == project_id,
                AudioTTSRequest.dependency_manifest_id.in_(
                    binding.dependency_manifest_id for binding, _, _ in rows
                ),
            )
        )
    }
    values = []
    for binding, version, _ in rows:
        used = None
        source = voices.get(version.creation_key) if version.creation_key else None
        if source:
            request, voice, manifest = source
            if (
                request.snapshot_id == binding.snapshot_id
                and request.line_id == binding.line_id
                and manifest.id == binding.dependency_manifest_id
                and request.target_asset_id == version.asset_id
            ):
                try:
                    selection = manifest_voice(manifest.snapshot_json.get("context", {}))
                except DomainError:
                    selection = None
                used = UsedVoice(
                    version_id=voice.id,
                    version_no=voice.version_no,
                    speaker=voice.speaker,
                    profile_version_id=(
                        selection.profile_version_id
                        if selection
                        and selection.voice_version_id == voice.id
                        and selection.episode_id == script.episode_id
                        and selection.character_asset_id == voice.character_asset_id
                        else None
                    ),
                )
        values.append(
            BoundAudio(
                version_id=version.id,
                line_id=binding.line_id,
                version_no=version.version_no,
                status=version.status,
                stale=version.stale,
                voice=used,
            )
        )
    return rows[0][2], values


def _shared_versions(db: Session, project_id: UUID, values: list[dict]) -> list[VersionLink]:
    ids = set()
    for value in values:
        for identity in reference_ids(value):
            try:
                ids.add(UUID(identity))
            except (TypeError, ValueError):
                continue
    if not ids:
        return []
    rows = db.execute(
        select(
            AssetVersion.id,
            AssetVersion.version_no,
            AssetVersion.stale,
            VisualAsset.id,
            VisualAsset.name,
        )
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(
            AssetVersion.id.in_(ids),
            VisualAsset.project_id == project_id,
            VisualAsset.asset_type.in_(["CHARACTER", "SCENE", "PROP", "STYLE"]),
        )
        .order_by(VisualAsset.name, AssetVersion.version_no, AssetVersion.id)
    )
    return [
        VersionLink(id=id, asset_id=asset, name=name, version_no=version, stale=stale)
        for id, version, stale, asset, name in rows
    ]


def series_episode(db: Session, episode_id: UUID, actor: str) -> SeriesEpisodeEvidence:
    facts = production_facts(db, episode_id, actor)
    state = episode_state(facts)
    result = SeriesEpisodeEvidence(
        project_id=state.project_id,
        episode_id=episode_id,
        phase=state.phase,
        issues=list(dict.fromkeys([*state.issues, *state.task_issues])),
        script_version_id=state.script_version_id,
        storyboard_version_id=state.storyboard_version_id,
    )
    script = db.get(Script, facts.script.version_id) if facts.script else None
    board = db.get(Storyboard, facts.storyboard.version_id) if facts.storyboard else None
    if script:
        try:
            script_content = read_script_content(script.content_json)
        except ValidationError:
            script_content = None
        if isinstance(script_content, ScriptContentV3):
            narrative = script_content.narrative_context
            outline = db.get(StoryOutline, narrative.series_plan_version_id)
            if outline and outline.project_id == facts.project_id:
                result.outline_version_id = outline.id
                result.outline_episode_id = outline.episode_id
                result.ledger_version_id = narrative.ledger_version_id
                result.target_story_time = narrative.target_story_time
        snapshot = db.scalar(
            select(AudioSnapshot)
            .where(
                AudioSnapshot.episode_id == episode_id, AudioSnapshot.script_version_id == script.id
            )
            .order_by(AudioSnapshot.created_at.desc(), AudioSnapshot.id.desc())
            .limit(1)
        )
        if snapshot:
            result.audio_snapshot_id = snapshot.id
            result.audio_line_count = snapshot.line_count
            result.bound_audio_total, result.bound_audio = _bound_audio(
                db, facts.project_id, script
            )
    if board:
        shot_facts = {shot.shot_id: shot for shot in facts.shots}
        current_timeline = bool(
            facts.timeline
            and not facts.timeline.stale
            and script
            and board.script_version_id == script.id
        )
        shots = select(Shot).where(Shot.id.in_(shot_facts)).order_by(Shot.shot_no)
        for row in db.scalars(shots):
            try:
                content = read_shot_content(row.content_json)
            except ValidationError:
                continue  # FLOW already reports invalid evidence; never count it as reviewed.
            if not isinstance(content, ShotContentV3):
                continue
            result.performance_shots += 1
            if any(item.presentation == "visible" for item in content.performance.dialogue):
                result.visible_dialogue_shots += 1
            required = any(item.lipsync == "required" for item in content.performance.dialogue)
            result.required_lip_sync_shots += int(required)
            dynamic = content.performance.motion_requirement == "dynamic_required"
            result.dynamic_shots += int(dynamic)
            evidence = shot_facts[row.id]
            if (
                current_timeline
                and evidence.media
                and evidence.media.kind == "VIDEO"
                and evidence.continuity
                and evidence.continuity.decision == "PASS"
                and evidence.selection
                and evidence.selection.decision == "APPROVE"
            ):
                # FLOW validates the exact video, temporal review and required
                # mouth correction (including its audio) during timeline admission.
                result.reviewed_dynamic_shots += int(dynamic)
                result.reviewed_videos.append(
                    ReviewedVideo(
                        version_id=evidence.media.version_id,
                        shot_no=row.shot_no,
                        temporal_review_id=evidence.continuity.review_id,
                        lip_sync_reviewed=required,
                    )
                )
    result.shared_versions = _shared_versions(
        db, facts.project_id, [row.content_json for row in (script, board) if row is not None]
    )
    if facts.render:
        result.delivery_id = db.scalar(
            select(RenderArtifact.id).where(
                RenderArtifact.asset_version_id == facts.render.version_id
            )
        )
        result.delivery_reviewed = state.phase in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    return result
