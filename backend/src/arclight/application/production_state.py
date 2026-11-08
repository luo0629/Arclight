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

MAX_PRODUCTION_ITEMS = 2000


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


def shot_facts(db: Session, episode: Episode, row: Shot) -> ShotFacts:
    issues: list[IssueCode] = []
    media = None
    file_check = None
    try:
        content = ShotContent.model_validate(row.content_json)
    except ValidationError:
        issues.append("INVALID_CONTENT")
    else:
        reference = content.selected_visual_asset
        if reference:
            found = db.execute(
                select(AssetVersion, VisualAsset)
                .join(VisualAsset, AssetVersion.asset_id == VisualAsset.id)
                .where(
                    AssetVersion.id == UUID(reference.asset_version_id),
                    VisualAsset.id == UUID(reference.asset_id),
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
    # Text approval and a generic quality reference cannot invent continuity or selection evidence.
    # Current jobs have no authoritative shot binding; arbitrary spec.context is not such a binding.
    return ShotFacts(
        project_id=episode.project_id,
        episode_id=episode.id,
        shot_id=row.id,
        storyboard_version_id=row.storyboard_id,
        issues=issues,
        media=media,
        file_check=file_check,
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
        shots=[shot_facts(db, episode, row) for row in shots],
        unbound_media_counts=TypeAdapter(dict[ExecutionStatus, int]).validate_python(
            {status: count for status, count in media_counts}
        ),
        unavailable_producers=["MEDIA_QA", "TIMELINE", "RENDER", "DELIVERY_QA"],
    )


def read_production_state(db: Session, episode_id: UUID, actor: str) -> EpisodeProductionState:
    return episode_state(production_facts(db, episode_id, actor))
