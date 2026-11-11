"""Retryable cleanup only for pre-registered, fenced, unsuccessful candidate objects."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.render_job_store import attempts, lock_job
from arclight.application.render_manifests import checked_snapshot
from arclight.core.config import Settings
from arclight.persistence.models import StoredFile
from arclight.persistence.render_manifests import RenderManifest
from arclight.storage.cleanup import identity
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)


def clean_candidates(factory: sessionmaker[Session], settings: Settings, run_id: UUID) -> None:
    try:
        _clean_candidates(factory, settings, run_id)
    except SQLAlchemyError:
        # A rolled-back cleanup retains PENDING/FAILED for the durable scanner.
        # Keep the render's original terminal result and discard native DB text.
        logger.warning("render_cleanup outcome=DATABASE_UNAVAILABLE")


def _clean_candidates(factory: sessionmaker[Session], settings: Settings, run_id: UUID) -> None:
    store = ObjectStorage(settings)
    with factory.begin() as db:
        run, job = lock_job(db, run_id)
        manifest = db.get(RenderManifest, job.manifest_id)
        formal = manifest is not None and checked_snapshot(manifest).output.name == "FORMAL"
        for attempt in attempts(db, run_id):
            if attempt.status in {"RUNNING", "SUCCEEDED"} or attempt.cleanup_status == "CLEANED":
                continue
            try:
                if run.storage_identity != identity(store):
                    attempt.cleanup_status = "FAILED"
                    attempt.cleanup_error_code = "RENDER_STORAGE_CHANGED"
                    continue
                expected = f"rendered/{job.project_id}/{attempt.id}"
                if (
                    attempt.output_key != expected
                    or db.scalar(select(StoredFile.id).where(StoredFile.object_key == expected))
                    is not None
                ):
                    attempt.cleanup_status = "FAILED"
                    attempt.cleanup_error_code = "RENDER_CLEANUP_PROTECTED"
                    continue
                if formal:
                    from arclight.application.render_reports import machine_report_key

                    report_key = machine_report_key(job.project_id, attempt.id)
                    if (
                        db.scalar(select(StoredFile.id).where(StoredFile.object_key == report_key))
                        is not None
                    ):
                        attempt.cleanup_status = "FAILED"
                        attempt.cleanup_error_code = "RENDER_CLEANUP_PROTECTED"
                        continue
                    store.delete(report_key)
                store.delete(expected)
                attempt.cleanup_status, attempt.cleanup_error_code = "CLEANED", None
            except Exception:
                # Native storage diagnostics may contain credentials, paths or signed URLs.
                attempt.cleanup_status, attempt.cleanup_error_code = (
                    "FAILED",
                    "RENDER_CLEANUP_FAILED",
                )
