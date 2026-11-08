"""Validate review contracts against existing project, candidate and version authority."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference
from arclight.application.art_proposals import visual_version_sha256
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.commands import require_project
from arclight.application.content_references import require_asset
from arclight.application.dependencies import exact_version, require_manifest
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import (
    REVIEW_SCHEMA_VERSION,
    REVIEW_VERSION,
    ContinuityReview,
    ReviewReference,
    ReviewTarget,
    review_schema_registration,
)
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import ProductionComponent
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    GenerationJob,
    ProductionVersion,
    Shot,
    Storyboard,
    VisualBibleVersion,
)


def shot_sha256(shot: Shot) -> str:
    return digest(
        {
            "storyboard_id": str(shot.storyboard_id),
            "shot_no": shot.shot_no,
            "duration_ms": shot.duration_ms,
            "description": shot.description,
            "content": shot.content_json,
        }
    )


def validate_review_versions(db: Session, review: ContinuityReview) -> None:
    for entry in review.versions:
        schema = review_schema_registration(costume=entry.version == REVIEW_SCHEMA_VERSION)
        if (
            entry.kind == "SCHEMA"
            and entry.version == REVIEW_VERSION
            and any(ref.asset_type == "COSTUME" for ref in review.references)
        ):
            raise DomainError("REVIEW_VERSION_INVALID", "服装参考须显式使用兼容扩展Schema版本", 409)
        row = db.get(ProductionVersion, entry.id)
        if (
            row is None
            or row.project_id != review.project_id
            or digest(row.definition_json) != row.digest
            or entry
            != ProductionComponent.model_validate(
                {
                    "id": row.id,
                    "family": row.family,
                    "kind": row.kind,
                    "name": row.name,
                    "version": row.version,
                    "digest": row.digest,
                }
            )
        ):
            raise DomainError("REVIEW_VERSION_INVALID", "审查精确版本登记不存在或不匹配", 409)
        if entry.kind == "SCHEMA" and (
            row.name != schema.name
            or row.version != schema.version
            or row.definition_json != schema.definition
        ):
            raise DomainError("REVIEW_VERSION_INVALID", "审查Schema与已声明合同不兼容", 409)


def require_review_target(
    db: Session, project_id: UUID, episode_id: UUID, target: ReviewTarget, actor: str
) -> tuple[AssetVersion, Storyboard, Shot]:
    """Share read-only candidate, media-check and exact shot admission."""
    project = require_project(db, project_id, actor)
    if project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目不能登记新审查结果", 409)
    episode = db.get(Episode, episode_id, populate_existing=True)
    if episode is None or episode.project_id != project.id:
        raise DomainError("REVIEW_SCOPE_INVALID", "审查剧集不属于当前项目", 422)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集不能登记新审查结果", 409)
    candidate = db.get(GenerationCandidate, target.candidate_id)
    check = require_candidate_media_check(db, project.id, target.candidate_id)
    assert candidate is not None
    version, asset = exact_version(db, project.id, target.asset_version_id)
    job = db.get(GenerationJob, candidate.job_id)
    if (
        candidate.asset_version_id != version.id
        or candidate.input_manifest_id != target.input_manifest_id
        or asset.id != target.asset_id
        or asset.asset_type != "SHOT_IMAGE"
        or version.stale
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
        or job is None
        or job.episode_id not in {None, episode.id}
        or target.media_check_sha256 != digest(check.model_dump(mode="json"))
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "审查候选与精确检查、资产或剧集不匹配", 409)
    board = db.get(Storyboard, target.storyboard_id, populate_existing=True)
    shot = db.get(Shot, target.shot_id, populate_existing=True)
    if (
        board is None
        or board.episode_id != episode.id
        or board.status != "APPROVED"
        or shot is None
        or shot.storyboard_id != board.id
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "审查须引用本剧集已批准的精确分镜与镜头", 409)
    return version, board, shot


def validate_review_references(
    db: Session,
    project_id: UUID,
    episode_id: UUID,
    target: ReviewTarget,
    references: list[ReviewReference],
    actor: str,
) -> None:
    """Check exact evidence independently of whether a review has completed."""
    version, board, shot = require_review_target(db, project_id, episode_id, target, actor)
    for ref in references:
        if ref.role == "CANDIDATE":
            checksum = version.checksum
        elif ref.role == "TARGET_SHOT":
            checksum = shot_sha256(shot)
        elif ref.role == "STORYBOARD":
            checksum = digest(board.content_json)
        elif ref.role == "ADJACENT_SHOT":
            adjacent = db.get(Shot, ref.resource_id)
            if (
                adjacent is None
                or adjacent.storyboard_id != board.id
                or abs(adjacent.shot_no - shot.shot_no) != 1
            ):
                raise DomainError("REVIEW_REFERENCE_INVALID", "相邻镜头须属于同一精确分镜", 422)
            checksum = shot_sha256(adjacent)
        elif ref.role == "VISUAL_BIBLE":
            bible = db.get(VisualBibleVersion, ref.resource_id)
            if bible is None or bible.project_id != project_id:
                raise DomainError("REVIEW_REFERENCE_INVALID", "视觉规范不属于当前项目", 422)
            checksum = visual_version_sha256(bible)
        else:
            assert ref.asset_id is not None and ref.asset_type is not None
            require_asset(
                db,
                project_id,
                AssetReference(asset_id=str(ref.asset_id), asset_version_id=str(ref.resource_id)),
                {ref.asset_type},
            )
            source, _ = exact_version(db, project_id, ref.resource_id)
            checksum = source.checksum
        if checksum != ref.content_sha256:
            raise DomainError("REVIEW_REFERENCE_CHANGED", "审查引用摘要与精确版本不一致", 409)
    manifest = require_manifest(db, project_id, target.input_manifest_id)
    declared = {
        str(ref.resource_id) for ref in references if ref.role in {"REFERENCE", "STORYBOARD"}
    }
    required = {item["source_asset_version_id"] for item in manifest.snapshot_json["inputs"]}
    if not required <= declared:
        raise DomainError("REVIEW_REFERENCE_MISSING", "审查不能省略候选生成清单的精确参考", 422)


def validate_review_contract(db: Session, review: ContinuityReview, actor: str) -> None:
    """Read-only validation; a valid contract grants neither review completion nor selection."""
    review = ContinuityReview.model_validate(review.model_dump(mode="json"))
    validate_review_references(
        db, review.project_id, review.episode_id, review.target, review.references, actor
    )
    validate_review_versions(db, review)
