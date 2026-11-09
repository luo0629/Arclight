"""Fail-closed shot admission shared by lifecycle, selection and storyboard inputs."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ShotContent
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.continuity_review_store import REVIEW_RESOURCE, require_review_record
from arclight.application.continuity_reviews import validate_review_contract
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.core.errors import DomainError
from arclight.domain.continuity_escalations import ESCALATION_RESOURCE, ContinuityEscalation
from arclight.domain.continuity_reviews import ReviewContractRecord
from arclight.domain.continuity_selection import (
    ADMISSION_RESOURCE,
    HUMAN_REVIEW_HANDLER,
    ReviewAdmission,
)
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    Project,
    Shot,
    VersionBinding,
    VisualAsset,
)


def validate_selected_shot(
    db: Session, record: ReviewContractRecord, content: ShotContent, episode_id: UUID | None
) -> None:
    source = db.get(Shot, record.review.target.shot_id)
    assert source is not None  # require_shot_review already validated the exact target.
    ignored = {"selected_visual_asset", "quality_review"}
    original = ShotContent.model_validate(source.content_json)
    if record.review.episode_id != episode_id or original.model_dump(
        mode="json", exclude=ignored
    ) != content.model_dump(mode="json", exclude=ignored):
        raise DomainError(
            "REVIEW_TARGET_INVALID", "选图须保留已审查的精确剧集和镜头意图，变更后须重新审查", 409
        )


def require_human_acceptance(
    db: Session, record: ReviewContractRecord, admission: ReviewAdmission, version: AssetVersion
) -> None:
    request = (
        db.get(HumanReviewRequest, admission.human_review_request_id)
        if admission.human_review_request_id is not None
        else None
    )
    decision = (
        db.scalar(select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id))
        if request is not None
        else None
    )
    try:
        if request is None or decision is None:
            raise ValueError("Missing human request or decision")
        snapshot = ReviewSnapshot.model_validate(request.snapshot_json)
        answer = HumanDecision.model_validate(decision.decision_json)
        review = record.review
        if (
            request.project_id != review.project_id
            or request.episode_id != review.episode_id
            or request.run_id != snapshot.run_id
            or request.handler != HUMAN_REVIEW_HANDLER
            or request.request_sha256 != digest(snapshot.model_dump(mode="json"))
            or snapshot.project_id != review.project_id
            or snapshot.episode_id != review.episode_id
            or snapshot.kind != "EXCEPTIONAL_SHOT"
            or snapshot.purpose != "PRODUCTION"
            or snapshot.object_id != review.result_id
            or snapshot.version_id != version.id
            or snapshot.content_sha256 != version.checksum
            or snapshot.input_sha256 != admission.record_sha256
            or answer.version_id != version.id
            or answer.request_sha256 != request.request_sha256
            or answer.decision != "APPROVE"
            or not answer.comment.strip()
        ):
            raise ValueError("Human acceptance must name this exact review and candidate")
        # Reuse the shared Owner/account authority check; revoked decisions do
        # not authorize a new selection. No request is created or resumed here.
        from arclight.application.human_reviews import require_review_actor

        require_review_actor(db, request.project_id, decision.actor_id, owner=True)
        escalation = find_binding(db, review.project_id, ESCALATION_RESOURCE, review.result_id)
        if escalation is not None:
            pause = ContinuityEscalation.model_validate(
                checked_snapshot(db, escalation)["continuity_escalation"]
            )
            require_review_actor(db, request.project_id, pause.requested_by, owner=False)
            resumed = db.scalar(
                select(HumanReviewResume).where(HumanReviewResume.request_id == request.id)
            )
            if (
                resumed is None
                or resumed.decision_id != decision.id
                or resumed.result_json.get("status") != "APPROVED"
            ):
                raise ValueError("The exact production exception must have a committed handoff")
    except (ValidationError, ValueError, TypeError) as exc:
        raise DomainError(
            "HUMAN_DECISION_REQUIRED", "候选须有绑定本次精确审查的人工批准及处理依据", 409
        ) from exc


def require_shot_review(db: Session, version: AssetVersion) -> ReviewContractRecord:
    """Validate durable evidence without changing any status or granting selection.

    Callers enforce actor permissions and serialize formal writes on the existing
    project gate. The project owner below is used only for QA-02's scope validation.
    Historical pointers stay readable; missing historical checks never imply PASS.
    """
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True)
    candidate = db.scalar(
        select(GenerationCandidate).where(GenerationCandidate.asset_version_id == version.id)
    )
    if asset is None or candidate is None or candidate.project_id != asset.project_id:
        if version.metadata_json.get("restored_from_version_id") is not None:
            # Preserve GEN-12's public error contract; a restored copy still
            # needs its own candidate/check rather than borrowing the original.
            raise DomainError(
                "CONTINUITY_REVIEW_REQUIRED", "恢复副本须先取得独立媒体检查并重新审查", 409
            )
        raise DomainError("MEDIA_CHECK_REQUIRED", "镜头图片缺少精确候选及媒体检查记录", 409)
    require_candidate_media_check(db, asset.project_id, candidate.id)
    rows = list(
        db.scalars(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == asset.project_id,
                VersionBinding.resource_type == REVIEW_RESOURCE,
                VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                    "candidate_id"
                ].as_string()
                == str(candidate.id),
            )
            .order_by(
                VersionBinding.snapshot_json["review_contract"]["sequence"].as_integer().desc()
            )
        )
    )
    if not rows:
        raise DomainError("CONTINUITY_REVIEW_REQUIRED", "候选图片须先完成必需的连续性审查", 409)
    project = db.get(Project, asset.project_id)
    assert project is not None
    record = require_review_record(db, project.id, rows[0].resource_id, project.owner_id)
    review = record.review
    # Until a workflow supplies authoritative shot binding, ambiguous target
    # claims must not let a caller choose whichever review happens to pass.
    if any(
        row.snapshot_json["review_contract"]["review"]["target"]
        != review.target.model_dump(mode="json")
        for row in rows
    ):
        raise DomainError("REVIEW_TARGET_AMBIGUOUS", "同一候选存在不一致的精确镜头审查目标", 409)
    validate_review_contract(db, review, project.owner_id)
    from arclight.application.continuity_escalations import require_completed_after_escalation

    require_completed_after_escalation(db, record)
    if review.target.asset_version_id != version.id or review.target.asset_id != asset.id:
        raise DomainError("REVIEW_TARGET_INVALID", "审查与选用的精确图片不匹配", 409)
    binding = find_binding(db, project.id, ADMISSION_RESOURCE, review.result_id)
    if binding is None:
        raise DomainError(
            "CONTINUITY_REVIEW_REQUIRED", "审查合同尚未由权威领域服务确认，不能正式选用", 409
        )
    try:
        snapshot = checked_snapshot(db, binding)
        admission = ReviewAdmission.model_validate(snapshot["review_admission"])
        if (
            admission.review_id != review.result_id
            or admission.record_sha256 != digest(record.model_dump(mode="json"))
            or snapshot["components"] != [item.model_dump(mode="json") for item in review.versions]
        ):
            raise ValueError("Admission must bind the exact latest receipt and versions")
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        raise DomainError(
            "REVIEW_ADMISSION_INVALID", "审查准入记录格式或精确版本不匹配", 409
        ) from exc
    from arclight.application.continuity_feedback_store import require_feedback_gate

    require_feedback_gate(db, project.id, candidate.id, review.result_id)
    if review.conclusion == "REVISION_REQUIRED":
        raise DomainError("CONTINUITY_REVISION_REQUIRED", "候选须修订并对新版本重新审查", 409)
    if (
        review.conclusion == "HUMAN_REVIEW"
        or admission.requires_human_review
        or admission.human_review_request_id is not None
    ):
        if admission.human_review_request_id is None:
            raise DomainError("HUMAN_DECISION_REQUIRED", "候选缺少本次审查必需的人工决定", 409)
        require_human_acceptance(db, record, admission, version)
    return record
