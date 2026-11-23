"""Finish a production review only after its exact candidate is explicitly selected."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit
from arclight.application.continuity_workflow import read_work
from arclight.application.generation_candidates import requires_continuity_review
from arclight.application.production_versions import checked_snapshot
from arclight.domain.agent_state import restore_agent_state, transition_agent_step
from arclight.domain.continuity_reviews import ReviewContractRecord
from arclight.domain.continuity_workflow import REVIEW_WORK_RESOURCE, CandidateReviewWork
from arclight.domain.video_review import VideoReviewRecord
from arclight.domain.video_temporal_reviewer import TEMPORAL_WORKFLOW_VERSION, TemporalWork
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
    version = db.get(AssetVersion, version_id)
    asset = db.get(VisualAsset, version.asset_id) if version is not None else None
    if (
        version is None
        or asset is None
        or asset.project_id != project_id
        or (asset.asset_type != "VIDEO" and not requires_continuity_review(version, asset))
    ):
        return  # Ordinary references do not depend on the later generation tables.
    candidate = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.asset_version_id == version_id,
        )
    )
    if candidate is None:
        return
    work: CandidateReviewWork | TemporalWork | None
    if asset.asset_type == "VIDEO":
        from arclight.application.video_review_work import read_temporal_work

        work = read_temporal_work(db, project_id, candidate.id)
    else:
        work = read_work(db, project_id, candidate.id)
    if work is not None:
        db.get(WorkflowRun, work.run_id, populate_existing=True, with_for_update=True)


def complete_selected_review(
    db: Session, record: ReviewContractRecord, *, short_drama_run_id: UUID | None = None
) -> None:
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
    finish_review(
        db,
        run,
        review.project_id,
        {
            "review_id": str(review.result_id),
            "candidate_id": str(review.target.candidate_id),
            "asset_version_id": str(version.id),
            "reason": "SHORT_DRAMA_SELECTED_IMAGE"
            if short_drama_run_id
            else "OWNER_SELECTED_REVIEWED_IMAGE",
            **({"short_drama_run_id": str(short_drama_run_id)} if short_drama_run_id else {}),
        },
    )


def complete_selected_video_review(
    db: Session, record: VideoReviewRecord, *, short_drama_run_id: UUID | None = None
) -> None:
    from arclight.application.video_review_work import read_temporal_work

    report = record.report
    work = read_temporal_work(db, report.project_id, report.target.candidate_id)
    if work is None or work.target != report.target:
        return
    run = db.get(WorkflowRun, work.run_id, populate_existing=True, with_for_update=True)
    if (
        run is None
        or run.workflow_version != TEMPORAL_WORKFLOW_VERSION
        or run.status != "WAITING_REVIEW"
        or (run.pending_review or {}).get("report_id") != str(report.report_id)
    ):
        return
    finish_review(
        db,
        run,
        report.project_id,
        {
            "review_id": str(report.report_id),
            "candidate_id": str(report.target.candidate_id),
            "asset_version_id": str(report.target.asset_version_id),
            "reason": "SHORT_DRAMA_SELECTED_VIDEO"
            if short_drama_run_id
            else "OWNER_SELECTED_REVIEWED_VIDEO",
            **({"short_drama_run_id": str(short_drama_run_id)} if short_drama_run_id else {}),
        },
    )


def finish_review(db: Session, run: WorkflowRun, project_id: UUID, event: dict) -> None:
    scope = scope_for_run(run, project_id)
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
    emit(db, run, "workflow.finished", event)
