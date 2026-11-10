"""Execute admitted manifests without holding database locks across media decoding."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.render_admission import admission_facts, validate_render_objects
from arclight.application.render_cleanup import clean_candidates
from arclight.application.render_inspections import inspect_candidate
from arclight.application.render_job_store import lock_job
from arclight.application.render_leases import POLICY, claim, finish, keep_alive, observe, owned
from arclight.application.render_plans import build_plan, check_support
from arclight.application.render_reviews import require_export_approval
from arclight.application.render_versions import register_render_version
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.render_plan import RENDER_TIMEOUT
from arclight.persistence.models import (
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
)
from arclight.persistence.render_manifests import RenderManifest
from arclight.storage.cleanup import identity
from arclight.storage.objects import ObjectStorage


def admitted(db: Session, settings: Settings, run: RenderExecution, job: RenderJob):
    require_project(db, job.project_id, run.created_by, write=True)
    if run.storage_identity != identity(ObjectStorage(settings)):
        raise DomainError("RENDER_STORAGE_CHANGED", "存储配置已改变", 409)
    row = db.get(RenderManifest, job.manifest_id, populate_existing=True)
    if row is None or row.content_sha256 != job.manifest_sha256:
        raise DomainError("RENDER_INPUT_CHANGED", "精确渲染清单已变化", 409)
    facts = admission_facts(db, row)
    check_support(facts.snapshot)
    require_export_approval(db, row, facts)
    return facts


def execute_render(factory: sessionmaker[Session], settings: Settings, run_id: UUID) -> str:
    token = claim(factory, run_id, POLICY)
    if token is None:
        clean_candidates(factory, settings, run_id)
        with factory() as db:
            run = db.get(RenderExecution, run_id)
            assert run is not None
            return run.status
    try:
        with keep_alive(factory, run_id, token, POLICY):
            clean_candidates(factory, settings, run_id)
            with factory.begin() as db:
                run, job = lock_job(db, run_id)
                owned(db, run, job, token)
                facts = admitted(db, settings, run, job)
                attempt = db.get(RenderAttempt, token)
                assert attempt is not None
                attempt.input_sha256 = facts.input_sha256
            observe(factory, run_id, token, phase="VALIDATING")

            def observer(progress: int | None) -> None:
                observe(factory, run_id, token, progress)

            with factory() as db:
                validate_render_objects(db, settings, facts, observer=observer)
                plan = build_plan(db, settings, facts.snapshot, observer=observer)
            observe(factory, run_id, token, phase="ENCODING")
            movie = execute_media(
                "render",
                "timeline",
                plan,
                timeout=RENDER_TIMEOUT,
                observer=observer,
            )
            observe(factory, run_id, token, phase="REVALIDATING")
            with factory() as db:
                validate_render_objects(db, settings, facts, observer=observer)
            # Preserve current admission errors and avoid expensive decoding for an
            # obsolete job. The same gate still runs immediately before publication.
            with factory.begin() as db:
                run, job = lock_job(db, run_id)
                owned(db, run, job, token)
                current = admitted(db, settings, run, job)
                if current.input_sha256 != facts.input_sha256:
                    raise DomainError("RENDER_INPUT_CHANGED", "渲染期间输入事实已变化", 409)
            observe(factory, run_id, token, phase="INSPECTING")
            inspection = inspect_candidate(
                movie,
                facts.snapshot.output,
                facts.snapshot.timeline.duration_ms,
                observer=observer,
            )
            # Commit evidence independently of the later object write. Even a
            # rejected candidate or a storage failure keeps its technical receipt.
            with factory.begin() as db:
                run, job = lock_job(db, run_id)
                owned(db, run, job, token)
                if job.cancel_requested_at:
                    raise DomainError("RENDER_CANCELLED", "渲染已请求取消", 409)
                current = admitted(db, settings, run, job)
                if current.input_sha256 != facts.input_sha256:
                    raise DomainError("RENDER_INPUT_CHANGED", "渲染期间输入事实已变化", 409)
                db.add(
                    RenderInspection(
                        attempt_id=token,
                        manifest_id=job.manifest_id,
                        manifest_sha256=job.manifest_sha256,
                        input_sha256=facts.input_sha256,
                        output_sha256=inspection.output_sha256,
                        byte_size=inspection.byte_size,
                        passed=inspection.passed,
                        result_json=inspection.model_dump(mode="json", round_trip=True),
                    )
                )
            if inspection.error_code:
                raise DomainError(inspection.error_code, "渲染产物未通过技术检查", 422)
            # Re-read exact source objects after encoding, before the final short
            # database gate. No business row lock is held across media decoding.
            observe(factory, run_id, token, phase="REVALIDATING")
            with factory() as db:
                validate_render_objects(db, settings, facts, observer=observer)
            observe(factory, run_id, token, phase="PERSISTING")
            with factory.begin() as db:
                run, job = lock_job(db, run_id)
                owned(db, run, job, token)
                if job.cancel_requested_at:
                    raise DomainError("RENDER_CANCELLED", "渲染已请求取消", 409)
                current = admitted(db, settings, run, job)
                if current.input_sha256 != facts.input_sha256:
                    raise DomainError("RENDER_INPUT_CHANGED", "渲染期间输入事实已变化", 409)
                attempt = db.get(RenderAttempt, token)
                assert attempt is not None
                store = ObjectStorage(settings)
                # Serialize the only candidate write with cancellation and claim recovery.
                # If this transaction fails, its durable attempt key still enables cleanup.
                store.put(attempt.output_key, movie, "video/mp4")
                owned(db, run, job, token)
                artifact = register_render_version(
                    db, settings, run, job, attempt, facts.snapshot, inspection
                )
                run.output_file_id = artifact.file_id
                attempt.progress, job.progress = 100, 100
                attempt.cleanup_status = "RETAINED"
                finish(db, run, job, "SUCCEEDED", None)
                audit(
                    db,
                    job.project_id,
                    run.created_by,
                    "render.succeeded",
                    run_id,
                    {
                        "manifest_id": str(job.manifest_id),
                        "output_file_id": str(artifact.file_id),
                        "asset_version_id": str(artifact.asset_version_id),
                        "artifact_id": str(artifact.id),
                    },
                )
    except Exception as exc:
        with factory.begin() as db:
            run, job = lock_job(db, run_id)
            # A stale worker cannot finish, clear, or overwrite its replacement's attempt.
            try:
                owned(db, run, job, token)
            except DomainError:
                pass  # Expired claims belong to recovery, even before replacement.
            else:
                cancelled = job.cancel_requested_at is not None
                code = (
                    exc.code
                    if isinstance(exc, DomainError)
                    else f"RENDER_MEDIA_{exc.reason}"
                    if isinstance(exc, MediaExecutionError)
                    else "RENDER_FAILED"
                )
                finish(
                    db,
                    run,
                    job,
                    "CANCELLED" if cancelled else "FAILED",
                    None if cancelled else code,
                )
                audit(
                    db,
                    job.project_id,
                    "renderer",
                    "render.stopped",
                    run_id,
                    {"status": run.status, "error_code": run.error_code},
                )
    finally:
        clean_candidates(factory, settings, run_id)
    with factory() as db:
        run = db.get(RenderExecution, run_id)
        assert run is not None
        return run.status
