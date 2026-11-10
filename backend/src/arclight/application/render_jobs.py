"""Authorized, idempotent render submission and revision-controlled cancellation."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_leases import clock
from arclight.application.render_admission import admission_facts
from arclight.application.render_job_store import lock_job, read_job
from arclight.application.render_manifests import manifest_row
from arclight.application.render_plans import check_support
from arclight.application.render_reviews import require_export_approval
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.render_jobs import RenderCancel, RenderJobCreate, RenderJobRead
from arclight.domain.versions import digest
from arclight.persistence.models import RenderExecution, RenderJob
from arclight.storage.cleanup import identity
from arclight.storage.objects import ObjectStorage


def create_job(
    db: Session, settings: Settings, manifest_id: UUID, actor: str, payload: RenderJobCreate
) -> RenderJobRead:
    row = manifest_row(db, manifest_id, actor, write=True)
    request_hash = digest(
        {
            "manifest_id": str(manifest_id),
            "retry_of": str(payload.retry_of) if payload.retry_of else None,
        }
    )
    previous = db.scalar(
        select(RenderJob).where(
            RenderJob.project_id == row.project_id, RenderJob.creation_key == payload.creation_key
        )
    )
    if previous is not None:
        run = db.get(RenderExecution, previous.id)
        assert run is not None
        if previous.request_sha256 != request_hash or run.created_by != actor:
            raise DomainError("IDEMPOTENCY_CONFLICT", "渲染请求键已用于其他输入或创建者", 409)
        return read_job(db, previous.id, actor)
    facts = admission_facts(db, row)
    check_support(facts.snapshot)
    require_export_approval(db, row, facts)
    if payload.retry_of is not None:
        original, job = (
            db.get(RenderExecution, payload.retry_of),
            db.get(RenderJob, payload.retry_of),
        )
        if (
            original is None
            or job is None
            or job.project_id != row.project_id
            or job.manifest_id != manifest_id
        ):
            raise DomainError("NOT_FOUND", "本清单的原渲染任务不存在", 404)
        if original.status not in {"FAILED", "CANCELLED"}:
            raise DomainError("RENDER_RETRY_CONFLICT", "只能另建失败或已取消任务的重渲染", 409)
    first = next(
        source.file
        for source in facts.snapshot.sources
        if source.file and source.asset_type in {"IMAGE", "SHOT_IMAGE"}
    )
    assert first is not None
    run_id = uuid4()
    db.add(
        RenderExecution(
            id=run_id,
            project_id=row.project_id,
            input_file_id=first.file_id,
            retry_of_id=payload.retry_of,
            created_by=actor,
            storage_identity=identity(ObjectStorage(settings)),
            intermediate_key=f"intermediate/{row.project_id}/{run_id}/{uuid4()}",
        )
    )
    db.flush()
    db.add(
        RenderJob(
            id=run_id,
            manifest_id=manifest_id,
            manifest_sha256=row.content_sha256,
            project_id=row.project_id,
            creation_key=payload.creation_key,
            request_sha256=request_hash,
        )
    )
    db.flush()
    audit(
        db,
        row.project_id,
        actor,
        "render.requested",
        run_id,
        {"manifest_id": str(manifest_id), "sha256": row.content_sha256},
    )
    # The QUEUED extension is the durable dispatch intent, scanned until claimed.
    return read_job(db, run_id, actor)


def cancel_job(db: Session, run_id: UUID, actor: str, payload: RenderCancel) -> RenderJobRead:
    run, job = lock_job(db, run_id)
    require_project(db, job.project_id, actor, write=True)
    if payload.expected_revision != job.revision:
        raise DomainError("REVISION_CONFLICT", "渲染状态已变化，请刷新后重试", 409)
    if run.status == "CANCELLED" or job.cancel_requested_at is not None:
        return read_job(db, run_id, actor)
    if run.status not in {"QUEUED", "RUNNING"}:
        raise DomainError("RENDER_CANCEL_CONFLICT", "已完成的渲染不能取消", 409)
    stamp = clock(db)
    job.cancel_requested_at, job.revision = stamp, job.revision + 1
    if run.status == "QUEUED":
        run.status, run.finished_at, job.phase = "CANCELLED", stamp, "CANCELLED"
    audit(db, job.project_id, actor, "render.cancel_requested", run_id)
    db.flush()
    return read_job(db, run_id, actor)
