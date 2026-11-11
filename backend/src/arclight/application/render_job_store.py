"""The shared lock order and safe projections for the existing render execution ledger."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.render_manifests import checked_snapshot
from arclight.core.errors import DomainError
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.domain.render_jobs import RenderAttemptRead, RenderJobRead
from arclight.persistence.models import (
    Project,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
)
from arclight.persistence.render_manifests import RenderArtifact, RenderManifest, RenderReportLink


def lock_job(db: Session, run_id: UUID) -> tuple[RenderExecution, RenderJob]:
    job = db.get(RenderJob, run_id)
    if job is None:
        raise DomainError("NOT_FOUND", "渲染任务不存在", 404)
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        assert driver is not None
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    # Recovery/cleanup must still work after archival, deletion or revoked membership.
    db.get(Project, job.project_id, populate_existing=True, with_for_update=True)
    run = db.get(RenderExecution, run_id, populate_existing=True, with_for_update=True)
    job = db.get(RenderJob, run_id, populate_existing=True, with_for_update=True)
    assert run is not None and job is not None
    return run, job


def attempts(db: Session, run_id: UUID) -> list[RenderAttempt]:
    return list(
        db.scalars(
            select(RenderAttempt)
            .where(RenderAttempt.run_id == run_id)
            .order_by(RenderAttempt.attempt_no)
        )
    )


def read_job(db: Session, run_id: UUID, actor: str) -> RenderJobRead:
    job, run = db.get(RenderJob, run_id), db.get(RenderExecution, run_id)
    if job is None or run is None:
        raise DomainError("NOT_FOUND", "渲染任务不存在", 404)
    require_project(db, job.project_id, actor)
    manifest = db.get(RenderManifest, job.manifest_id)
    assert manifest is not None
    history = attempts(db, run_id)
    cleanup = "NOT_REQUIRED"
    if history:
        cleanup = next(
            (
                state
                for state in ("FAILED", "PENDING")
                if any(a.cleanup_status == state for a in history)
            ),
            "RETAINED" if run.status == "SUCCEEDED" else "CLEANED",
        )
    artifact = db.scalar(select(RenderArtifact).where(RenderArtifact.job_id == run_id))
    # Import locally: report registration shares the execution ledger's lock order.
    from arclight.application.render_reports import delivery_acceptance

    acceptance = delivery_acceptance(db, artifact, actor) if artifact else None
    return RenderJobRead(
        id=run_id,
        manifest_id=job.manifest_id,
        manifest_sha256=job.manifest_sha256,
        retry_of_id=run.retry_of_id,
        profile=checked_snapshot(manifest).output.name,
        status=run.status,
        revision=job.revision,
        progress=job.progress,
        phase=job.phase,
        attempt_count=job.attempt_count,
        cancel_requested_at=job.cancel_requested_at,
        output_file_id=run.output_file_id,
        output_asset_version_id=artifact.asset_version_id if artifact else None,
        artifact_id=artifact.id if artifact else None,
        is_delivery=acceptance.is_delivery if acceptance else False,
        delivery_phase=acceptance.phase if acceptance else None,
        report_id=db.scalar(
            select(RenderReportLink.id).where(RenderReportLink.artifact_id == artifact.id)
        )
        if artifact
        else None,
        error_code=run.error_code,
        cleanup_status=cleanup,
        created_at=run.created_at,
        started_at=job.started_at,
        finished_at=run.finished_at,
    )


def read_attempts(db: Session, run_id: UUID, actor: str) -> list[RenderAttemptRead]:
    read_job(db, run_id, actor)
    receipts = {
        row.attempt_id: row
        for row in db.scalars(
            select(RenderInspection)
            .join(RenderAttempt, RenderAttempt.id == RenderInspection.attempt_id)
            .where(RenderAttempt.run_id == run_id)
        )
    }
    result = []
    for row in attempts(db, run_id):
        value = RenderAttemptRead.model_validate(row, from_attributes=True)
        if row.id in receipts:
            value.inspection = RenderInspectionResult.model_validate(receipts[row.id].result_json)
        result.append(value)
    return result
