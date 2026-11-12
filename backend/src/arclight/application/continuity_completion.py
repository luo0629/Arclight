"""Finish a production review only after its latest image is explicitly selected."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit
from arclight.application.continuity_workflow import read_work
from arclight.application.production_versions import checked_snapshot
from arclight.domain.agent_state import restore_agent_state, transition_agent_step
from arclight.domain.continuity_reviews import ReviewContractRecord
from arclight.domain.continuity_workflow import REVIEW_WORK_RESOURCE, CandidateReviewWork
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    VersionBinding,
    VisualAsset,
    WorkflowRun,
)


def lock_selection_run(db: Session, project_id: UUID, version_id: UUID) -> None:
    """Take the Reviewer run lock before the caller's shared project write gate."""
    from arclight.application.production_events import begin_serialized

    begin_serialized(db)
    candidate = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.asset_version_id == version_id,
        )
    )
    work = read_work(db, project_id, candidate.id) if candidate is not None else None
    if work is not None:
        db.get(WorkflowRun, work.run_id, populate_existing=True, with_for_update=True)


def complete_selected_review(db: Session, record: ReviewContractRecord) -> None:
    """Called inside the Owner selection transaction, after admission and CAS checks."""
    review = record.review
    work = read_work(db, review.project_id, review.target.candidate_id)
    if work is None:
        return  # Historical reviews have no production workflow to finish.
    run = db.get(WorkflowRun, work.run_id, populate_existing=True, with_for_update=True)
    if run is None or run.workflow_type != "continuity_review" or run.status != "WAITING_REVIEW":
        return
    latest = db.scalar(
        select(VersionBinding)
        .where(
            VersionBinding.project_id == review.project_id,
            VersionBinding.resource_type == REVIEW_WORK_RESOURCE,
            VersionBinding.snapshot_json["continuity_work"]["run_id"].as_string() == str(run.id),
        )
        .order_by(VersionBinding.created_at.desc(), VersionBinding.id.desc())
        .limit(1)
    )
    if (
        latest is None
        or CandidateReviewWork.model_validate(checked_snapshot(db, latest)["continuity_work"])
        != work
    ):
        return
    asset = db.get(VisualAsset, review.target.asset_id)
    version = db.get(AssetVersion, review.target.asset_version_id)
    if (
        asset is None
        or version is None
        or asset.current_version_id != version.id
        or version.status != "APPROVED"
        or version.stale
    ):
        return
    scope = scope_for_run(run, review.project_id)
    state = restore_agent_state(run.agent_state_json, None, scope)
    run.agent_state_json = transition_agent_step(
        state,
        scope,
        expected_revision=state.revision,
        step_id="storyboard",
        target="SUCCEEDED",
        termination_reason="COMPLETED",
    ).model_dump(mode="json")
    run.status, run.pending_review = "SUCCEEDED", None
    run.revision += 1
    emit(
        db,
        run,
        "workflow.finished",
        {
            "review_id": str(review.result_id),
            "candidate_id": str(review.target.candidate_id),
            "asset_version_id": str(version.id),
            "reason": "OWNER_SELECTED_REVIEWED_IMAGE",
        },
    )
