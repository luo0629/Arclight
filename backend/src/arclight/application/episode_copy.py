"""Copy an episode's usable text, never its workflow, approval or rendered results."""

from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    AssetReference,
    ScriptContentV3,
    ShotContentV3,
    read_script_content,
    read_storyboard_content,
)
from arclight.application.content_references import require_asset
from arclight.core.errors import DomainError
from arclight.persistence.models import AuditEvent, Episode, Script, Shot, Storyboard

COPYABLE_STATUSES = ("DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED")


def copy_episode(
    db: Session, source: Episode, actor: str, *, episode_no: int, title: str, reuse_assets: bool
) -> Episode:
    # Pick one usable script and only its own latest usable storyboard, not unrelated history.
    script = db.scalar(
        select(Script)
        .where(Script.episode_id == source.id, Script.status.in_(COPYABLE_STATUSES))
        .order_by(Script.version_no.desc())
        .limit(1)
    )
    board = (
        db.scalar(
            select(Storyboard)
            .where(
                Storyboard.episode_id == source.id,
                Storyboard.script_version_id == script.id,
                Storyboard.status.in_(COPYABLE_STATUSES),
            )
            .order_by(Storyboard.version_no.desc())
            .limit(1)
        )
        if script is not None
        else None
    )
    reused: set[str] = set()

    def asset(reference: AssetReference | None, kinds: set[str]) -> AssetReference | None:
        if not reuse_assets or reference is None:
            return None
        try:
            require_asset(db, source.project_id, reference, kinds)
        except DomainError as exc:
            if exc.code not in {"INVALID_ASSET_REFERENCE", "ASSET_NOT_USABLE"}:
                raise
            return None
        reused.add(reference.asset_version_id)
        return reference

    try:
        text = read_script_content(script.content_json) if script else None
        shots = read_storyboard_content(board.content_json) if board else None
        if text and shots:
            shots.validate_against(text)
    except (ValidationError, ValueError):
        raise DomainError(
            "INVALID_COPY_SOURCE", "原剧集文本引用不完整，请先另存修正版本", 409
        ) from None

    if text:
        scenes: dict[str, str] = {}
        lines: dict[str, str] = {}
        bindings = []
        for binding in text.character_assets:
            reference = asset(binding.asset, {"CHARACTER"})
            if reference:
                binding.asset = reference
                bindings.append(binding)
        text.character_assets = bindings
        for scene in text.scenes:
            old_id = scene.scene_id
            scene.scene_id = str(uuid4())
            scenes[old_id] = scene.scene_id
            for line in scene.dialogue:
                old_id = line.line_id
                line.line_id = str(uuid4())
                lines[old_id] = line.line_id
            scene.scene_asset = asset(scene.scene_asset, {"SCENE"})
            scene.location_asset = asset(scene.location_asset, {"LOCATION", "SCENE"})
            scene.props = [ref for item in scene.props if (ref := asset(item, {"PROP"}))]
        if isinstance(text, ScriptContentV3):
            proposal_ids = {
                proposal.id: uuid4() for scene in text.scenes for proposal in scene.state_changes
            }
            for scene in text.scenes:
                for interaction in scene.dramatic_beat.dialogue_interactions:
                    interaction.line_ids = [
                        UUID(lines[str(identity)]) for identity in interaction.line_ids
                    ]
                for proposal in scene.state_changes:
                    proposal.id = proposal_ids[proposal.id]
                    proposal.causes = [
                        proposal_ids.get(identity, identity) for identity in proposal.causes
                    ]
            # The original context is retained as evidence only. Approving this copied
            # draft requires explicitly saving a new context for the target episode.
        if shots:
            for shot in shots.shots:
                shot.scene_id = scenes[shot.scene_id] if shot.scene_id else None
                shot.dialogue_line_ids = [lines[line] for line in shot.dialogue_line_ids]
                if isinstance(shot, ShotContentV3):
                    for delivery in shot.performance.dialogue:
                        delivery.line_id = lines[delivery.line_id]
                for detail in shot.character_details:
                    detail.asset = asset(detail.asset, {"CHARACTER"})
                    detail.costume = asset(detail.costume, {"COSTUME"})
                shot.scene_asset = asset(shot.scene_asset, {"SCENE"})
                shot.location_asset = asset(shot.location_asset, {"LOCATION", "SCENE"})
                shot.props = [ref for item in shot.props if (ref := asset(item, {"PROP"}))]
                if isinstance(shot, ShotContentV3):
                    retained_props = {item.asset_id: item for item in shot.props}
                    shot.performance.props = [
                        item
                        for item in shot.performance.props
                        if retained_props.get(item.asset.asset_id) == item.asset
                    ]
                # Shot results and their approvals belong to the old production, not this draft.
                shot.selected_visual_asset = None
                shot.quality_review = None
            shots.validate_against(text)

    target = Episode(
        project_id=source.project_id,
        episode_no=episode_no,
        title=title,
        target_duration_ms=source.target_duration_ms,
        status="DRAFT",
    )
    db.add(target)
    db.flush()
    if text:
        copied_script = Script(
            episode_id=target.id,
            run_id=None,
            version_no=1,
            content_json=text.model_dump(mode="json"),
            status="READY_FOR_REVIEW",
        )
        db.add(copied_script)
        db.flush()
        if shots:
            copied_board = Storyboard(
                episode_id=target.id,
                run_id=None,
                script_version_id=copied_script.id,
                version_no=1,
                content_json=shots.model_dump(mode="json"),
                status="READY_FOR_REVIEW",
            )
            db.add(copied_board)
            db.flush()
            for shot in shots.shots:
                db.add(
                    Shot(
                        storyboard_id=copied_board.id,
                        shot_no=shot.shot_no,
                        duration_ms=shot.duration_ms,
                        description=shot.visual_description,
                        content_json=shot.model_dump(mode="json"),
                        status="READY_FOR_REVIEW",
                    )
                )
    db.add(
        AuditEvent(
            project_id=source.project_id,
            actor_id=actor,
            action="episode.copied",
            resource_id=target.id,
            details_json={
                "source_episode_id": str(source.id),
                "source_script_id": str(script.id) if script else None,
                "source_storyboard_id": str(board.id) if board else None,
                "reuse_assets": reuse_assets,
                "reused_asset_version_ids": sorted(reused),
            },
        )
    )
    db.flush()
    return target
