"""Small, exact-version episode summary for the series section and stage navigator."""

from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    ScriptContentV3,
    ShotContentV3,
    read_script_content,
    read_shot_content,
)
from arclight.application.content_dependencies import reference_ids
from arclight.application.production_state import production_facts
from arclight.domain.production_status import episode_state
from arclight.persistence.audio_production import AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    Script,
    Shot,
    Storyboard,
    StoryOutline,
    VisualAsset,
)
from arclight.persistence.render_manifests import RenderArtifact


class VersionLink(BaseModel):
    id: UUID
    asset_id: UUID
    name: str
    version_no: int
    stale: bool


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
    audio_snapshot_id: UUID | None = None
    audio_line_count: int | None = None
    delivery_id: UUID | None = None
    delivery_reviewed: bool = False
    shared_versions: list[VersionLink] = Field(default_factory=list)


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
    if board:
        shot_facts = {shot.shot_id: shot for shot in facts.shots}
        for row in db.scalars(select(Shot).where(Shot.id.in_(shot_facts))):
            try:
                content = read_shot_content(row.content_json)
            except ValidationError:
                continue  # FLOW already reports invalid evidence; never count it as reviewed.
            if not isinstance(content, ShotContentV3):
                continue
            result.performance_shots += 1
            if any(item.presentation == "visible" for item in content.performance.dialogue):
                result.visible_dialogue_shots += 1
            if content.performance.motion_requirement == "dynamic_required":
                result.dynamic_shots += 1
                evidence = shot_facts[row.id]
                if (
                    evidence.media
                    and evidence.media.kind == "VIDEO"
                    and evidence.continuity
                    and evidence.continuity.decision == "PASS"
                    and evidence.selection
                    and evidence.selection.decision == "APPROVE"
                ):
                    result.reviewed_dynamic_shots += 1
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
