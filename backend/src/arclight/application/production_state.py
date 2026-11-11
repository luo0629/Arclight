"""Project-authorized production reads over existing text, dependency and job records."""

from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, ShotContent, StoryboardContent
from arclight.application.commands import require_episode
from arclight.application.content_dependencies import reference_ids
from arclight.application.content_references import (
    validate_script_assets,
    validate_storyboard_assets,
)
from arclight.application.project_reads import lock_project_snapshot
from arclight.core.errors import DomainError
from arclight.domain.enums import AssetStatus
from arclight.domain.production_state import (
    EpisodeFacts,
    EpisodeProductionState,
    ExecutionEvidence,
    ExecutionStatus,
    IssueCode,
    ReviewEvidence,
    ShotFacts,
    VersionEvidence,
)
from arclight.domain.production_status import episode_state
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    Episode,
    Script,
    Shot,
    StoredFile,
    Storyboard,
    VisualAsset,
    WorkflowRun,
)
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack

MAX_PRODUCTION_ITEMS = 2000


def timeline_evidence(db: Session, episode: Episode) -> VersionEvidence | None:
    row = db.scalar(
        select(Timeline)
        .where(Timeline.episode_id == episode.id, Timeline.sealed.is_(True))
        .order_by(Timeline.version_no.desc())
        .limit(1)
    )
    if row is None:
        return None
    version = db.get(AssetVersion, row.id, populate_existing=True)
    if version is None:
        raise DomainError("TIMELINE_HISTORY_INVALID", "时间轴的精确版本登记缺失", 409)
    sources = {row.storyboard_id}
    for clip in db.scalars(select(TimelineClip).where(TimelineClip.timeline_id == row.id)):
        sources.add(clip.source_asset_version_id)
        if clip.script_version_id:
            sources.add(clip.script_version_id)
        if clip.audio_version_id:
            sources.add(clip.audio_version_id)
    return VersionEvidence(
        project_id=episode.project_id,
        episode_id=episode.id,
        version_id=row.id,
        kind="TIMELINE",
        status=AssetStatus(version.status),
        stale=version.stale or version.status in {"STALE", "ARCHIVED"},
        source_version_ids=sorted(sources),
    )


def text_version(db: Session, episode: Episode, row: Script | Storyboard) -> VersionEvidence:
    registry = db.get(AssetVersion, row.id, populate_existing=True)
    status = row.status
    if (
        status in {"APPROVED", "LOCKED"}
        and registry is not None
        and registry.status not in {"APPROVED", "LOCKED"}
    ):
        status = registry.status
    return VersionEvidence(
        project_id=episode.project_id,
        episode_id=episode.id,
        version_id=row.id,
        kind="SCRIPT" if isinstance(row, Script) else "STORYBOARD",
        status=AssetStatus(status),
        stale=bool(registry and (registry.stale or registry.status in {"STALE", "ARCHIVED"}))
        or row.status in {"STALE", "ARCHIVED"},
        source_version_ids=[row.script_version_id] if isinstance(row, Storyboard) else [],
    )


def latest_execution(
    db: Session,
    episode: Episode,
    types: tuple[str, ...],
    artifact: Script | Storyboard | None,
    *,
    source_script: Script | None = None,
) -> ExecutionEvidence | None:
    query = select(WorkflowRun).where(
        WorkflowRun.episode_id == episode.id, WorkflowRun.workflow_type.in_(types)
    )
    if source_script is not None:
        # A director attempt is relevant only to the exact current script. Combined
        # legacy runs bind through the script they actually produced, not their age.
        query = query.where(
            or_(
                WorkflowRun.input_json["script_version_id"].as_string() == str(source_script.id),
                WorkflowRun.id == source_script.run_id,
            )
        )
    row = db.scalar(query.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).limit(1))
    if row is None:
        return None
    # An earlier failed attempt does not invalidate a newer, independently saved artifact.
    if artifact is not None and row.id != artifact.run_id and row.created_at <= artifact.created_at:
        return None
    return ExecutionEvidence.model_validate(
        {
            "project_id": episode.project_id,
            "episode_id": episode.id,
            "execution_id": row.id,
            "target_id": episode.id,
            "status": row.status,
        }
    )


def input_issues(
    db: Session, episode: Episode, script: Script | None, board: Storyboard | None
) -> list[IssueCode]:
    issues: list[IssueCode] = []
    for row in (script, board):
        if row is None:
            continue
        if db.get(AssetVersion, row.id) is None:
            issues.append("MISSING_VERSION_REGISTRY")
        try:
            checked_content: ScriptContent | StoryboardContent
            if isinstance(row, Script):
                script_content = ScriptContent.model_validate(row.content_json)
                checked_content = script_content
                validate_script_assets(db, episode.project_id, script_content)
            else:
                board_content = StoryboardContent.model_validate(row.content_json)
                # Selected media is a later-stage gate. Validate the same reference
                # assets here without confusing a draft candidate with missing art.
                prerequisites = board_content.model_copy(
                    update={
                        "shots": [
                            shot.model_copy(
                                update={"selected_visual_asset": None, "quality_review": None}
                            )
                            for shot in board_content.shots
                        ]
                    }
                )
                checked_content = prerequisites
                validate_storyboard_assets(
                    db, episode.project_id, prerequisites, episode_id=episode.id
                )
        except ValidationError:
            issues.append("INVALID_CONTENT")
        except DomainError as error:
            # Use the same authoritative reference gate as saves and reviews.
            if error.code == "ASSET_NOT_USABLE":
                identities = [
                    UUID(value) for value in reference_ids(checked_content.model_dump(mode="json"))
                ]
                stale = db.scalar(
                    select(AssetVersion.id)
                    .join(VisualAsset, AssetVersion.asset_id == VisualAsset.id)
                    .where(
                        VisualAsset.project_id == episode.project_id,
                        AssetVersion.id.in_(identities),
                        AssetVersion.stale.is_(True),
                    )
                    .limit(1)
                )
                issues.append("STALE_INPUT" if stale else "ASSET_NOT_APPROVED")
            else:
                issues.append("MISSING_ASSET")
    return list(dict.fromkeys(issues))


def shot_facts(
    db: Session, episode: Episode, row: Shot, clip: TimelineClip | None = None
) -> ShotFacts:
    issues: list[IssueCode] = []
    media = None
    file_check = None
    continuity = None
    selection = None
    try:
        content = ShotContent.model_validate(row.content_json)
    except ValidationError:
        issues.append("INVALID_CONTENT")
    else:
        reference = content.selected_visual_asset
        selected = (
            db.get(AssetVersion, clip.source_asset_version_id)
            if clip is not None and clip.shot_id == row.id
            else None
        )
        if reference or selected:
            if reference:
                version_id, asset_id = UUID(reference.asset_version_id), UUID(reference.asset_id)
            else:
                assert selected is not None
                version_id, asset_id = selected.id, selected.asset_id
            found = db.execute(
                select(AssetVersion, VisualAsset)
                .join(VisualAsset, AssetVersion.asset_id == VisualAsset.id)
                .where(
                    AssetVersion.id == version_id,
                    VisualAsset.id == asset_id,
                    VisualAsset.project_id == episode.project_id,
                    VisualAsset.asset_type.in_(("IMAGE", "SHOT_IMAGE")),
                )
            ).first()
            if found is None:
                issues.append("MISSING_ASSET")
            else:
                version, asset = found
                media = VersionEvidence(
                    project_id=episode.project_id,
                    episode_id=episode.id,
                    kind="IMAGE",
                    version_id=version.id,
                    status=AssetStatus(version.status),
                    stale=version.stale or asset.status in {"ARCHIVED", "STALE", "REJECTED"},
                    source_version_ids=list(
                        db.scalars(
                            select(AssetDependency.source_asset_version_id)
                            .where(AssetDependency.target_asset_version_id == version.id)
                            .distinct()
                            .order_by(AssetDependency.source_asset_version_id)
                        )
                    ),
                )
                receipt = db.execute(
                    select(GenerationCandidate, StoredFile)
                    .join(
                        StoredFile,
                        StoredFile.asset_version_id == GenerationCandidate.asset_version_id,
                    )
                    .where(
                        GenerationCandidate.project_id == episode.project_id,
                        GenerationCandidate.asset_version_id == version.id,
                        GenerationCandidate.error_code.is_(None),
                        StoredFile.project_id == episode.project_id,
                        StoredFile.checksum == version.checksum,
                        StoredFile.object_key == version.object_key,
                        StoredFile.media_type.in_(("image/png", "image/jpeg")),
                    )
                ).first()
                if receipt is not None:
                    candidate, _stored = receipt
                    file_check = ReviewEvidence(
                        project_id=episode.project_id,
                        episode_id=episode.id,
                        review_id=candidate.id,
                        version_id=version.id,
                        source_version_id=row.storyboard_id,
                        actor_id=candidate.created_by,
                        decision="PASS",
                    )
                if clip is not None and clip.shot_id == row.id:
                    from arclight.application.timelines import selected_clip_facts

                    try:
                        checked = selected_clip_facts(
                            db, episode, row, version.id, clip.ordinal, clip.duration_ms
                        )
                        if (
                            checked.review_id is not None
                            and checked.source_sha256 == clip.source_sha256
                            and checked.review_id == clip.review_id
                            and checked.review_storyboard_id == clip.review_storyboard_id
                            and checked.review_shot_id == clip.review_shot_id
                            and clip.source_asset_version_id == version.id
                        ):
                            # The sealed clip binds this exact media to the approved
                            # storyboard after the shared selection/review gate passes.
                            media = media.model_copy(
                                update={
                                    "source_version_ids": sorted(
                                        {*media.source_version_ids, row.storyboard_id}
                                    )
                                }
                            )
                            continuity = ReviewEvidence(
                                project_id=episode.project_id,
                                episode_id=episode.id,
                                review_id=checked.review_id,
                                version_id=version.id,
                                source_version_id=row.storyboard_id,
                                actor_id="continuity-selection",
                                decision="PASS",
                            )
                            selection = ReviewEvidence(
                                project_id=episode.project_id,
                                episode_id=episode.id,
                                review_id=clip.id,
                                version_id=version.id,
                                source_version_id=row.storyboard_id,
                                actor_id="timeline-selection",
                                decision="APPROVE",
                            )
                    except DomainError:
                        # A historical clip cannot grant a revoked or obsolete selection.
                        pass
    # Generic job context and text approval cannot invent continuity or selection proof.
    return ShotFacts(
        project_id=episode.project_id,
        episode_id=episode.id,
        shot_id=row.id,
        storyboard_version_id=row.storyboard_id,
        issues=issues,
        media=media,
        file_check=file_check,
        continuity=continuity,
        selection=selection,
    )


def production_facts(db: Session, episode_id: UUID, actor: str) -> EpisodeFacts:
    episode = require_episode(db, episode_id, actor)
    lock_project_snapshot(db, episode.project_id)
    db.refresh(episode)
    require_episode(db, episode.id, actor)
    script = db.scalar(
        select(Script)
        .where(Script.episode_id == episode.id)
        .order_by(Script.version_no.desc())
        .limit(1)
    )
    board = db.scalar(
        select(Storyboard)
        .where(Storyboard.episode_id == episode.id)
        .order_by(Storyboard.version_no.desc())
        .limit(1)
    )
    shots = (
        list(
            db.scalars(
                select(Shot)
                .where(Shot.storyboard_id == board.id)
                .order_by(Shot.shot_no, Shot.id)
                .limit(MAX_PRODUCTION_ITEMS + 1)
            )
        )
        if board
        else []
    )
    media_counts = db.execute(
        select(GenerationJob.status, func.count(GenerationJob.id))
        .where(
            GenerationJob.project_id == episode.project_id,
            GenerationJob.episode_id == episode.id,
        )
        .group_by(GenerationJob.status)
        .order_by(GenerationJob.status)
    ).all()
    if len(shots) > MAX_PRODUCTION_ITEMS:
        raise DomainError("PRODUCTION_STATE_LIMIT", "生产状态证据数量超限，请缩小生产范围", 413)
    from arclight.application.render_production import render_evidence

    timeline = timeline_evidence(db, episode)
    delivery = render_evidence(db, episode, timeline, actor)
    clips = (
        list(
            db.scalars(
                select(TimelineClip)
                .join(TimelineTrack, TimelineTrack.id == TimelineClip.track_id)
                .join(Timeline, Timeline.id == TimelineClip.timeline_id)
                .where(
                    Timeline.id == timeline.version_id,
                    Timeline.storyboard_id == board.id,
                    TimelineTrack.kind == "VIDEO",
                    TimelineClip.shot_id.is_not(None),
                )
                .order_by(TimelineClip.ordinal)
            )
        )
        if timeline and board
        else []
    )
    selections = {clip.shot_id: clip for clip in clips}
    if len(selections) != len(clips):
        # Ambiguous historical clips never establish a shot selection.
        selections = {}
    return EpisodeFacts(
        project_id=episode.project_id,
        episode_id=episode.id,
        archived=episode.status == "ARCHIVED",
        script=text_version(db, episode, script) if script else None,
        storyboard=text_version(db, episode, board) if board else None,
        input_issues=input_issues(
            db,
            episode,
            script,
            board if script and board and board.script_version_id == script.id else None,
        ),
        script_execution=latest_execution(
            db, episode, ("script_storyboard", "script_rewrite"), script
        ),
        storyboard_execution=latest_execution(
            db, episode, ("script_storyboard", "storyboard"), board, source_script=script
        )
        if script
        else None,
        shots=[shot_facts(db, episode, row, selections.get(row.id)) for row in shots],
        unbound_media_counts=TypeAdapter(dict[ExecutionStatus, int]).validate_python(
            {status: count for status, count in media_counts}
        ),
        timeline=timeline,
        render=delivery.render,
        render_validation=delivery.validation,
        delivery_review=delivery.review,
        warnings=delivery.warnings,
        warning_acceptance=delivery.acceptance,
        unavailable_producers=["MEDIA_QA"],
    )


def read_production_state(db: Session, episode_id: UUID, actor: str) -> EpisodeProductionState:
    return episode_state(production_facts(db, episode_id, actor))
