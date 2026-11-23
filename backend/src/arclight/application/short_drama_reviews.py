"""Exact service decisions made within an owner's short-drama authorization."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_approvals import apply_option
from arclight.application.art_protection import require_art_confirmation
from arclight.application.art_reviews import art_snapshot, selection
from arclight.application.art_workflow import delivered, review_target
from arclight.application.audit import audit
from arclight.application.commands import emit
from arclight.application.content_dependencies import content_approval_impact, review_registry
from arclight.application.production_versions import bind, find_binding
from arclight.application.short_drama_delivery import require_run_authorization
from arclight.application.text_reviews import validate_manual_approval
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.art_proposals import ArtProposalRecord, ArtProposalVersion
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import OutboxEvent, Shot, Storyboard, WorkflowRun

SERVICE_REVIEW = "SHORT_DRAMA_SERVICE_REVIEW"


def review_receipt(root: WorkflowRun, child: WorkflowRun, target: dict) -> dict:
    return {
        "root_run_id": str(root.id),
        "child_run_id": str(child.id),
        "authorization_sha256": digest(root.input_json["short_drama"]),
        "child_input_sha256": digest(child.input_json),
        "target": target,
    }


def verify_service_review(db: Session, child: WorkflowRun, target: dict) -> None:
    control = db.get(AgentRunLimit, child.id)
    root = db.get(WorkflowRun, control.parent_run_id) if control and control.parent_run_id else None
    if root is None or control is None or root.id != control.root_run_id:
        raise DomainError("SHORT_DRAMA_REVIEW_SCOPE", "自动检查必须属于原短剧制作运行", 409)
    episode, _, _ = require_run_authorization(db, root)
    if (
        root.status != "RUNNING"
        or episode.project_id != control.project_id
        or child.episode_id != root.episode_id
    ):
        raise DomainError("SHORT_DRAMA_REVIEW_SCOPE", "自动检查必须属于原短剧制作运行", 409)
    saved = find_binding(db, control.project_id, SERVICE_REVIEW, child.id)
    if saved is None or saved.snapshot_json != review_receipt(root, child, target):
        raise DomainError("SHORT_DRAMA_REVIEW_SCOPE", "自动检查与本次精确制作证据不一致", 409)


def queue_checked(
    db: Session, root: WorkflowRun, child: WorkflowRun, project_id: UUID, target: dict
) -> None:
    bind(db, project_id, SERVICE_REVIEW, child.id, review_receipt(root, child, target))
    verify_service_review(db, child, target)
    child.resume_json = {**target, "decision": "APPROVE", "authority": "SHORT_DRAMA"}
    child.pending_review = None
    child.status = "QUEUED"
    child.revision += 1
    db.add(OutboxEvent(run_id=child.id, revision=child.revision))
    audit(
        db,
        project_id,
        "short-drama-service",
        "short_drama.inputs_checked",
        child.id,
        {
            "authorized_by": root.input_json["created_by"],
            "run_id": str(root.id),
            **target,
        },
    )
    emit(db, child, "short_drama.inputs_checked", target)


def apply_art_selection(
    db: Session, root: WorkflowRun, child: WorkflowRun, project_id: UUID
) -> bool:
    art = delivered(db, child.id)
    if art is None:
        # Specialist questions share WAITING_REVIEW with a delivered proposal.
        return False
    target = review_target(art)
    row = db.get(ArtProposalRecord, art.proposal_id)
    version = db.get(ArtProposalVersion, art.version_id)
    request = db.get(HumanReviewRequest, art.review_request_id)
    if (
        row is None
        or version is None
        or request is None
        or row.project_id != project_id
        or version.proposal_id != row.id
        or child.pending_review != target
        or request.run_id != child.id
        or db.scalar(
            select(HumanReviewDecision.id).where(HumanReviewDecision.request_id == request.id)
        )
        is not None
    ):
        raise DomainError("SHORT_DRAMA_ART_CHANGED", "原美术提案或审核已变化", 409)
    snapshot = art_snapshot(db, row, version).model_dump(mode="json")
    if snapshot != request.snapshot_json or digest(snapshot) != request.request_sha256:
        raise DomainError("SHORT_DRAMA_ART_CHANGED", "原美术提案或审核已变化", 409)
    actor = root.input_json["created_by"]
    confirmation = selection(db, version.id)
    option = require_art_confirmation(
        db, ArtProposal.model_validate(version.proposal_json), actor, confirmation
    )
    if any(change.protection != "NONE" for change in option.changes):
        raise DomainError("SHORT_DRAMA_ART_PROTECTED", "美术提案涉及已批准或锁定的设计", 409)
    apply_option(db, version, confirmation, actor, authority="SHORT_DRAMA")
    queue_checked(
        db,
        root,
        child,
        project_id,
        {
            **target,
            "request_sha256": request.request_sha256,
        },
    )
    return True


def approve_board(
    db: Session, settings: Settings, root: WorkflowRun, child: WorkflowRun, project_id: UUID
) -> bool:
    pending = child.pending_review or {}
    if pending.get("resource_type") != "storyboard":
        return False
    board = db.get(Storyboard, UUID(pending["version_id"]), with_for_update=True)
    if (
        board is None
        or board.run_id != child.id
        or board.episode_id != root.episode_id
        or str(board.script_version_id) != root.input_json["short_drama"]["script_version_id"]
        or board.status != "READY_FOR_REVIEW"
    ):
        raise DomainError("SHORT_DRAMA_BOARD_SCOPE", "待检查分镜与本次定稿剧本不一致", 409)
    validate_manual_approval(
        db, board, settings, target_duration_ms=root.input_json["short_drama"]["target_duration_ms"]
    )
    impact = content_approval_impact(db, board)
    if impact["affected"]:
        raise DomainError("SHORT_DRAMA_BOARD_IMPACT", "自动批准会使已有分镜或媒体失效", 409)
    board.status = "APPROVED"
    for shot in db.scalars(select(Shot).where(Shot.storyboard_id == board.id)):
        shot.status = board.status
    review_registry(db, board, root.input_json["created_by"], impact["digest"])
    queue_checked(
        db, root, child, project_id, {**pending, "content_sha256": digest(board.content_json)}
    )
    return True
