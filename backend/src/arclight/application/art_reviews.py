"""ART-04 adapter for FLOW-03; requests bind one immutable version and option."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_approvals import apply_option
from arclight.application.art_proposal_store import (
    check_revision,
    current_version,
    require_proposal,
)
from arclight.application.art_protection import compare_art_proposal, require_art_confirmation
from arclight.application.human_reviews import (
    ReviewHandler,
    request_review,
    resume_review,
    submit_decision,
)
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal, VersionArtSource
from arclight.domain.art_protection import ArtChangeLocation, ArtComparison, ArtConfirmation
from arclight.domain.art_review import ArtDecision, ArtSubmit
from arclight.domain.human_reviews import HumanDecision, ReviewResume, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import (
    ArtProposalRecord,
    ArtProposalReview,
    ArtProposalVersion,
)
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import Project


def request_id(version: ArtProposalVersion, project_id: UUID) -> UUID:
    return uuid5(project_id, "human-review/1:art:" + str(version.id))


def selection(db: Session, version_id: UUID) -> ArtConfirmation:
    bound = db.scalar(select(ArtProposalReview).where(ArtProposalReview.version_id == version_id))
    if bound is None:
        raise DomainError("ART_REVIEW_REQUIRED", "该提案版本尚未提交审核", 409)
    return ArtConfirmation.model_validate(bound.confirmation_json)


def art_snapshot(
    db: Session, row: ArtProposalRecord, version: ArtProposalVersion
) -> ReviewSnapshot:
    proposal = ArtProposal.model_validate(version.proposal_json)
    project = db.get(Project, row.project_id, populate_existing=True)
    if project is None or row.archived or version.version_no != row.version_no:
        raise DomainError("REVIEW_INPUT_CHANGED", "提案已归档或有新的修订", 409)
    comparison = compare_art_proposal(db, proposal, project.owner_id)
    if comparison.model_dump(mode="json") != version.comparison_json:
        raise DomainError("REVIEW_INPUT_CHANGED", "提案精确输入或锁定状态已经变化", 409)
    confirmation = selection(db, version.id)
    from arclight.application.art_workflow import proposal_run

    run = proposal_run(db, row, version)
    return ReviewSnapshot(
        project_id=row.project_id,
        episode_id=proposal.episode_id,
        run_id=run.id if run else None,
        thread_id=str(run.id) if run else "art:" + str(version.id),
        kind="VISUAL_SPEC"
        if any(
            design.kind == "VISUAL_BIBLE"
            for option in proposal.options
            for design in option.designs
        )
        else "REFERENCE",
        object_id=row.id,
        version_id=version.id,
        content_sha256=comparison.proposal_sha256,
        input_sha256=digest(
            {"input": version.input_sha256, "confirmation": confirmation.model_dump(mode="json")}
        ),
        source_version_ids=sorted(
            {
                source.resource_id
                for source in proposal.sources
                if isinstance(source, VersionArtSource)
            },
            key=str,
        ),
        expected_revision=row.revision,
    )


def read_art(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    row = db.get(ArtProposalRecord, frozen.object_id, populate_existing=True)
    version = db.get(ArtProposalVersion, frozen.version_id, populate_existing=True)
    if row is None or version is None or version.proposal_id != row.id:
        raise DomainError("REVIEW_INPUT_CHANGED", "提案精确对象不存在", 409)
    return art_snapshot(db, row, version)


def submit_art(
    db: Session, project_id: UUID, proposal_id: UUID, actor: str, payload: ArtSubmit
) -> HumanReviewRequest:
    row = require_proposal(db, project_id, proposal_id, actor, write=True)
    check_revision(row, payload.expected_revision)
    version = current_version(db, row)
    if version.id != payload.version_id:
        raise DomainError("VERSION_CONFLICT", "请提交当前精确提案版本", 409)
    comparison = ArtComparison.model_validate(version.comparison_json)
    option = next(
        (item for item in comparison.options if item.option_id == payload.option_id), None
    )
    if option is None or payload.comparison_sha256 != comparison.comparison_sha256:
        raise DomainError("ART_COMPARISON_STALE", "提交方案或比较基准不一致", 409)
    if not option.changes or comparison.unresolved_question_ids:
        raise DomainError("ART_REVIEW_NOT_READY", "请先解决未决问题并选择有变更的方案", 409)
    confirmation = ArtConfirmation(
        option_id=option.option_id,
        comparison_sha256=comparison.comparison_sha256,
        confirmed_changes=[
            ArtChangeLocation(design_id=change.design_id, field_path=change.field_path)
            for change in option.changes
        ],
    )
    prior = db.scalar(select(ArtProposalReview).where(ArtProposalReview.version_id == version.id))
    if prior is None:
        db.add(
            ArtProposalReview(
                version_id=version.id,
                created_by=actor,
                confirmation_json=confirmation.model_dump(mode="json"),
            )
        )
        db.flush()
    elif prior.confirmation_json != confirmation.model_dump(mode="json"):
        raise DomainError("ART_REVIEW_CONFLICT", "该提案版本已绑定另一个审核方案", 409)
    snapshot = art_snapshot(db, row, version)
    return request_review(db, actor, "art:" + str(version.id), "ART_PROPOSAL", snapshot)


def check_decision(
    db: Session, request: HumanReviewRequest, decision: HumanDecision, actor: str
) -> None:
    if decision.decision != "APPROVE":
        return
    frozen = ReviewSnapshot.model_validate(request.snapshot_json)
    version = db.get(ArtProposalVersion, frozen.version_id)
    assert version is not None
    confirmation = selection(db, version.id)
    if decision.impact_digest != digest(confirmation.model_dump(mode="json")):
        raise DomainError("ART_CONFIRMATION_REQUIRED", "须明确确认所选方案的全部变更字段", 409)
    require_art_confirmation(
        db, ArtProposal.model_validate(version.proposal_json), actor, confirmation
    )


def apply_art(
    db: Session, request: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    from arclight.application.art_workflow import queue_art_review

    queue_art_review(db, request, decision)
    value = HumanDecision.model_validate(decision.decision_json)
    check_decision(db, request, value, actor)
    if value.decision == "APPROVE":
        version = db.get(ArtProposalVersion, value.version_id)
        assert version is not None
        apply_option(db, version, selection(db, version.id), actor)
    return {"status": "APPROVED" if value.decision == "APPROVE" else "REJECTED"}


def decide_art(
    db: Session, project_id: UUID, proposal_id: UUID, actor: str, payload: ArtDecision
) -> HumanReviewResume:
    row = require_proposal(db, project_id, proposal_id, actor, write=True)
    check_revision(row, payload.expected_revision)
    version = current_version(db, row)
    if version.id != payload.version_id:
        raise DomainError("VERSION_CONFLICT", "决定须绑定当前精确提案版本", 409)
    confirmation = payload.confirmation
    if confirmation is not None and confirmation != selection(db, version.id):
        raise DomainError("ART_CONFIRMATION_REQUIRED", "确认字段与已提交的精确审核方案不一致", 409)
    review = request_id(version, project_id)
    saved = submit_decision(
        db,
        review,
        actor,
        HumanDecision(
            version_id=version.id,
            request_sha256=payload.request_sha256,
            decision=payload.decision,
            comment=payload.comment,
            impact_digest=digest(confirmation.model_dump(mode="json")) if confirmation else None,
        ),
    )
    return resume_review(
        db,
        review,
        actor,
        ReviewResume(
            decision_id=saved.id,
            request_sha256=payload.request_sha256,
        ),
    )


ART_HANDLERS = {"ART_PROPOSAL": ReviewHandler(read_art, apply_art, check_decision)}
