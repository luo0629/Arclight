"""Read-only, exact-version comparison of existing continuity receipts."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from arclight.api.asset_versions import VersionRead, find_version, read_version
from arclight.api.assets import AssetRead, find_asset
from arclight.api.dependencies import Actor, Database
from arclight.api.generation_candidates import CandidateRead, read_candidate
from arclight.api.human_reviews import HumanReviewRead, serialize
from arclight.api.routes import Limit, Offset
from arclight.application.asset_objects import read_object
from arclight.application.commands import require_project
from arclight.application.continuity_feedback_store import feedback_history
from arclight.application.continuity_review_store import REVIEW_RESOURCE, require_review_record
from arclight.application.continuity_revision_store import job_revision, revision_record
from arclight.application.continuity_selection import require_shot_review
from arclight.application.dependencies import exact_version
from arclight.core.errors import DomainError
from arclight.domain.continuity_escalations import INCOMPLETE_REVIEW_HANDLER
from arclight.domain.continuity_feedback import FEEDBACK_HANDLER, FeedbackRecord
from arclight.domain.continuity_reviews import ReviewContractRecord, ReviewReference
from arclight.domain.continuity_revisions import REVISION_RESOURCE, RevisionRecord
from arclight.domain.continuity_selection import HUMAN_REVIEW_HANDLER
from arclight.persistence.human_reviews import HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    GenerationJob,
    Project,
    ProjectMember,
    VersionBinding,
    VisualAsset,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/assets/{asset_id}/versions",
    tags=["continuity-comparison"],
)


class Blocker(BaseModel):
    code: str
    message: str


class ComparisonReference(BaseModel):
    reference: ReviewReference
    name: str
    image_path: str | None = None
    status: str | None = None
    unavailable_code: str | None = None


class ComparisonReview(BaseModel):
    record: ReviewContractRecord
    references: list[ComparisonReference]


class ComparisonImage(BaseModel):
    candidate_id: UUID | None
    asset_id: UUID
    asset_name: str
    version: VersionRead
    round: int
    revision_id: UUID | None
    image_path: str


class ComparisonRevision(BaseModel):
    record: RevisionRecord
    feedback: list[FeedbackRecord]


class ComparisonActions(BaseModel):
    owner: bool
    writable: bool
    blocker: Blocker | None
    can_submit: bool
    can_approve: bool
    can_reject: bool
    can_select: bool


class ComparisonHumanReview(HumanReviewRead):
    can_approve: bool


COMPARISON_REVIEW_HANDLERS = frozenset(
    {HUMAN_REVIEW_HANDLER, INCOMPLETE_REVIEW_HANDLER, FEEDBACK_HANDLER}
)


def comparison_access(db: Database, project: Project, actor: str) -> tuple[bool, bool]:
    owner = project.owner_id == actor
    member = (
        db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.user_id == UUID(actor)
            )
        )
        if not owner
        else None
    )
    writable = not project.archived and (owner or member is not None and member.role == "editor")
    return owner, writable


def comparison_human_review(
    db: Database, row: HumanReviewRequest, *, owner: bool, writable: bool
) -> ComparisonHumanReview:
    return ComparisonHumanReview(
        **serialize(db, row).model_dump(),
        can_approve=owner and writable and row.handler != INCOMPLETE_REVIEW_HANDLER,
    )


class ContinuityComparison(BaseModel):
    project_id: UUID
    asset: AssetRead
    version: VersionRead
    candidate: CandidateRead | None
    image_path: str
    latest_review_id: UUID | None
    reviews: list[ComparisonReview]
    next_offset: int | None
    images: list[ComparisonImage]
    images_truncated: bool
    revisions: list[ComparisonRevision]
    human_reviews: list[ComparisonHumanReview]
    next_human_offset: int | None
    actions: ComparisonActions


def image_path(project_id: UUID, version: AssetVersion) -> str:
    return f"/projects/{project_id}/assets/{version.asset_id}/versions/{version.id}/image"


def read_reference(
    db: Database, project_id: UUID, ref: ReviewReference, version: AssetVersion, review_id: UUID
) -> ComparisonReference:
    result = ComparisonReference(reference=ref, name=ref.role)
    if ref.role == "CANDIDATE":
        result.image_path = image_path(project_id, version)
        result.status = version.status
    elif ref.role == "REFERENCE":
        try:
            source, asset = exact_version(db, project_id, ref.resource_id)
            if (
                source.asset_id != ref.asset_id
                or asset.asset_type != ref.asset_type
                or source.checksum != ref.content_sha256
            ):
                raise DomainError("REVIEW_REFERENCE_CHANGED", "精确参考与审查摘要不一致")
            result.name = asset.name
            result.status = "STALE" if source.stale else source.status
            if source.metadata_json.get("storage_backend") in {"LOCAL", "S3"}:
                result.image_path = (
                    f"/projects/{project_id}/assets/{version.asset_id}/versions/{version.id}"
                    f"/continuity/reviews/{review_id}/references/{ref.reference_id}/image"
                )
        except DomainError as exc:
            # Keep the immutable citation visible without substituting a current version.
            result.unavailable_code = exc.code
    return result


def read_lineage(
    db: Database,
    project_id: UUID,
    candidate: GenerationCandidate | None,
    asset: VisualAsset,
    version: AssetVersion,
) -> tuple[list[ComparisonImage], bool, list[ComparisonRevision]]:
    root = candidate.id if candidate else None
    if candidate:
        job = db.get(GenerationJob, candidate.job_id)
        origin = job_revision(db, job) if job else None
        if origin:
            root = origin.root_candidate_id
    bindings = (
        list(
            db.scalars(
                select(VersionBinding)
                .where(
                    VersionBinding.project_id == project_id,
                    VersionBinding.resource_type == REVISION_RESOURCE,
                    VersionBinding.snapshot_json["continuity_revision"][
                        "root_candidate_id"
                    ].as_string()
                    == str(root),
                )
                .order_by(
                    VersionBinding.snapshot_json["continuity_revision"]["round"].as_integer(),
                    VersionBinding.id,
                )
                .limit(5)
            )
        )
        if root
        else []
    )
    # QA-05 permits three media rounds plus one exhausted receipt.
    if len(bindings) > 4:
        raise DomainError("REVISION_RECORD_INVALID", "修订谱系超过已冻结的轮次上限")
    revisions = [revision_record(db, project_id, row.resource_id) for row in bindings]
    origins: dict[UUID, RevisionRecord | None] = {
        item.job_id: item for item in revisions if item.job_id
    }
    comparison_jobs = set(origins)
    original = db.get(GenerationCandidate, root) if root else None
    if original is not None and original.project_id == project_id:
        comparison_jobs.add(original.job_id)
    # Include existing batch siblings, upload replacements and restored history,
    # even when no automated revision has been requested for this candidate.
    rows = list(
        db.execute(
            select(AssetVersion, GenerationCandidate, VisualAsset)
            .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
            .outerjoin(
                GenerationCandidate,
                (GenerationCandidate.asset_version_id == AssetVersion.id)
                & (GenerationCandidate.project_id == project_id),
            )
            .where(
                VisualAsset.project_id == project_id,
                (AssetVersion.asset_id == asset.id)
                | GenerationCandidate.job_id.in_(comparison_jobs),
            )
            .order_by(AssetVersion.created_at, AssetVersion.id)
            .limit(101)
        )
    )
    images = []
    for source, source_candidate, source_asset in rows[:100]:
        if source_candidate and source_candidate.job_id not in origins:
            job = db.get(GenerationJob, source_candidate.job_id)
            origins[source_candidate.job_id] = job_revision(db, job) if job else None
        revision = origins.get(source_candidate.job_id) if source_candidate else None
        images.append(
            ComparisonImage(
                candidate_id=source_candidate.id if source_candidate else None,
                asset_id=source.asset_id,
                asset_name=source_asset.name,
                version=read_version(source),
                round=revision.round if revision else 0,
                revision_id=revision.revision_id if revision else None,
                image_path=image_path(project_id, source),
            )
        )
    if not any(item.version.id == version.id for item in images):
        origin = origins.get(candidate.job_id) if candidate else None
        images.append(
            ComparisonImage(
                candidate_id=candidate.id if candidate else None,
                asset_id=asset.id,
                asset_name=asset.name,
                version=read_version(version),
                round=origin.round if origin else 0,
                revision_id=origin.revision_id if origin else None,
                image_path=image_path(project_id, version),
            )
        )
    return (
        images,
        len(rows) > 100,
        [
            ComparisonRevision(
                record=item, feedback=feedback_history(db, project_id, item.revision_id)
            )
            for item in revisions
        ],
    )


@router.get("/{version_id}/continuity")
def get_comparison(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
    human_offset: Offset = 0,
) -> ContinuityComparison:
    project = require_project(db, project_id, actor)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    version = find_version(db, asset_id, version_id)
    if asset.asset_type not in {"SHOT_IMAGE", "IMAGE"} or (
        version.metadata_json.get("required_review") == "ART_PROPOSAL"
    ):
        raise DomainError("NOT_FOUND", "镜头候选不存在", 404)
    candidate = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.asset_version_id == version_id,
        )
    )
    statement = (
        select(VersionBinding)
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == REVIEW_RESOURCE,
            VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                "candidate_id"
            ].as_string()
            == str(candidate.id if candidate else None),
        )
        .order_by(
            VersionBinding.snapshot_json["review_contract"]["sequence"].as_integer().desc(),
            VersionBinding.id,
        )
    )
    latest = db.scalar(statement.limit(1))
    rows = list(db.scalars(statement.limit(limit + 1).offset(offset)))
    reviews = []
    for row in rows[:limit]:
        record = require_review_record(db, project_id, row.resource_id, actor)
        if record.review.target.asset_version_id != version_id:
            raise DomainError("REVIEW_TARGET_INVALID", "历史审查不属于此精确图片")
        reviews.append(
            ComparisonReview(
                record=record,
                references=[
                    read_reference(db, project_id, ref, version, record.review.result_id)
                    for ref in record.review.references
                ],
            )
        )
    blocker = None
    try:
        require_shot_review(db, version)
    except DomainError as exc:
        blocker = Blocker(code=exc.code, message=exc.message)
    owner, writable = comparison_access(db, project, actor)
    editable = writable and asset.status not in {"ARCHIVED", "LOCKED"}
    usable = editable and not version.stale and blocker is None
    humans = list(
        db.scalars(
            select(HumanReviewRequest)
            .where(
                HumanReviewRequest.project_id == project_id,
                HumanReviewRequest.handler.in_(COMPARISON_REVIEW_HANDLERS),
                HumanReviewRequest.snapshot_json["version_id"].as_string() == str(version_id),
            )
            .order_by(HumanReviewRequest.created_at.desc(), HumanReviewRequest.id)
            .limit(limit + 1)
            .offset(human_offset)
        )
    )
    images, truncated, revisions = read_lineage(db, project_id, candidate, asset, version)
    return ContinuityComparison(
        project_id=project_id,
        asset=AssetRead.model_validate(asset),
        version=read_version(version),
        candidate=read_candidate(db, candidate) if candidate else None,
        image_path=image_path(project_id, version),
        latest_review_id=latest.resource_id if latest else None,
        reviews=reviews,
        next_offset=offset + limit if len(rows) > limit else None,
        images=images,
        images_truncated=truncated,
        revisions=revisions,
        human_reviews=[
            comparison_human_review(db, row, owner=owner, writable=writable)
            for row in humans[:limit]
        ],
        next_human_offset=human_offset + limit if len(humans) > limit else None,
        actions=ComparisonActions(
            owner=owner,
            writable=writable,
            blocker=blocker,
            can_submit=usable and version.status == "DRAFT",
            can_approve=usable and owner and version.status == "READY_FOR_REVIEW",
            can_reject=editable and owner and version.status == "READY_FOR_REVIEW",
            can_select=usable and owner and version.status == "APPROVED",
        ),
    )


@router.get("/{version_id}/continuity/human-reviews/{request_id}")
def get_comparison_human_review(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    request_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
) -> ComparisonHumanReview:
    """Read an exact request beyond a paged history, with the same approval hints."""
    project = require_project(db, project_id, actor)
    asset = find_asset(db, project_id, asset_id, include_media=True)
    version = find_version(db, asset_id, version_id)
    if asset.asset_type not in {"SHOT_IMAGE", "IMAGE"} or (
        version.metadata_json.get("required_review") == "ART_PROPOSAL"
    ):
        raise DomainError("NOT_FOUND", "镜头候选不存在", 404)
    row = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.id == request_id,
            HumanReviewRequest.project_id == project_id,
            HumanReviewRequest.handler.in_(COMPARISON_REVIEW_HANDLERS),
            HumanReviewRequest.snapshot_json["project_id"].as_string() == str(project_id),
            HumanReviewRequest.snapshot_json["kind"].as_string() == "EXCEPTIONAL_SHOT",
            HumanReviewRequest.snapshot_json["version_id"].as_string() == str(version_id),
        )
    )
    if row is None:
        raise DomainError("NOT_FOUND", "此精确图片没有该人工审核请求", 404)
    response.headers["Cache-Control"] = "private, no-store"
    owner, writable = comparison_access(db, project, actor)
    return comparison_human_review(db, row, owner=owner, writable=writable)


@router.get("/{version_id}/continuity/reviews/{review_id}/references/{reference_id}/image")
def get_reference_image(
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID,
    review_id: UUID,
    reference_id: UUID,
    request: Request,
    db: Database,
    actor: Actor,
) -> Response:
    """Read a frozen citation, including historical internal reference asset types."""
    require_project(db, project_id, actor)
    find_asset(db, project_id, asset_id, include_media=True)
    find_version(db, asset_id, version_id)
    record = require_review_record(db, project_id, review_id, actor)
    if (
        record.review.target.asset_id != asset_id
        or record.review.target.asset_version_id != version_id
    ):
        raise DomainError("NOT_FOUND", "此精确图片没有该审查引用", 404)
    ref = next(
        (
            item
            for item in record.review.references
            if item.reference_id == reference_id and item.role == "REFERENCE"
        ),
        None,
    )
    if ref is None:
        raise DomainError("NOT_FOUND", "此轮审查没有该图片引用", 404)
    source, asset = exact_version(db, project_id, ref.resource_id)
    if (
        source.asset_id != ref.asset_id
        or asset.asset_type != ref.asset_type
        or source.checksum != ref.content_sha256
    ):
        raise DomainError("REVIEW_REFERENCE_CHANGED", "图片引用与原审查摘要不一致")
    media_type = source.metadata_json.get("media", {}).get("media_type")
    if source.metadata_json.get("storage_backend") not in {"LOCAL", "S3"} or media_type not in {
        "image/png",
        "image/jpeg",
    }:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "此精确参考尚无可读取的受支持原图")
    data = read_object(
        request.app.state.settings, project_id, source.id, source.object_key, ref.content_sha256
    )
    return Response(
        data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="review-reference"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
