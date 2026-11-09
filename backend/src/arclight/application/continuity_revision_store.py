"""Immutable revision receipts and execution guards shared by every media entry point."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.continuity_escalations import require_completed_after_escalation
from arclight.application.continuity_routing import read_routed_review
from arclight.application.human_reviews import require_review_actor
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_revisions import REVISION_RESOURCE, RevisionRecord, RevisionRequest
from arclight.domain.versions import digest
from arclight.persistence.models import DependencyManifest, GenerationJob, VersionBinding


def revision_record(db: Session, project_id: UUID, revision_id: UUID) -> RevisionRecord:
    binding = find_binding(db, project_id, REVISION_RESOURCE, revision_id)
    if binding is None:
        raise DomainError("NOT_FOUND", "修订记录不存在", 404)
    try:
        snapshot = checked_snapshot(db, binding)
        record = RevisionRecord.model_validate(snapshot["continuity_revision"])
        if (
            record.project_id != project_id
            or record.revision_id != revision_id
            or snapshot["parameters"] != record.request.model_dump(mode="json")
            or not any(
                entry["kind"] == "SCHEMA"
                and entry["name"] == "continuity-revision-request"
                and entry["digest"] == digest(RevisionRequest.model_json_schema())
                for entry in snapshot["components"]
            )
        ):
            raise ValueError("Revision receipt must retain its original command and schema")
    except (KeyError, ValueError, TypeError, ValidationError) as exc:
        raise DomainError("REVISION_RECORD_INVALID", "修订凭据与原请求或版本不一致", 409) from exc
    return record


def job_revision(db: Session, job: GenerationJob) -> RevisionRecord | None:
    manifest = db.get(DependencyManifest, job.input_manifest_id)
    context = manifest.snapshot_json.get("context") if manifest else None
    origin = context.get("continuity_revision") if isinstance(context, dict) else None
    if origin is None:
        return None
    try:
        record = revision_record(db, job.project_id, UUID(origin["revision_id"]))
        if (
            record.model_dump(mode="json", exclude={"job_id", "input_manifest_id"}) != origin
            or record.job_id != job.id
            or record.input_manifest_id != job.input_manifest_id
        ):
            raise ValueError("Revision must identify this exact job and manifest")
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("REVISION_RECORD_INVALID", "媒体任务缺少匹配的原修订凭据", 409) from exc
    return record


def candidate_revision(db: Session, project_id: UUID, candidate_id: UUID) -> VersionBinding | None:
    return db.scalar(
        select(VersionBinding).where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == REVISION_RESOURCE,
            VersionBinding.snapshot_json["continuity_revision"]["target"][
                "candidate_id"
            ].as_string()
            == str(candidate_id),
        )
    )


def require_revision_execution(db: Session, job: GenerationJob) -> None:
    record = job_revision(db, job)
    if record is None:
        return
    require_review_actor(db, record.project_id, record.created_by, owner=False)
    # Workers already own the job and project gate. Taking a workflow row lock here
    # would invert the workflow -> project order used by human handling/routing.
    reviewed, route = read_routed_review(db, record.request.review_id, lock_run=False)
    require_review_actor(db, record.project_id, route.routed_by, owner=False)
    require_completed_after_escalation(db, reviewed)
    if (
        reviewed.review.target != record.target
        or reviewed.review.conclusion != "REVISION_REQUIRED"
        or digest(reviewed.model_dump(mode="json")) != record.review_sha256
    ):
        raise DomainError("REVISION_SOURCE_INVALID", "修订的原审查或精确输入已不再有效", 409)

    from arclight.application.continuity_feedback_store import require_feedback_gate

    require_feedback_gate(
        db, record.project_id, record.target.candidate_id, reviewed.review.result_id
    )


def require_revision_target(db: Session, job: GenerationJob, target: ReviewTarget) -> None:
    record = job_revision(db, job)
    if record is not None and (
        target.asset_id != record.target.asset_id
        or target.storyboard_id != record.target.storyboard_id
        or target.shot_id != record.target.shot_id
    ):
        raise DomainError("REVISION_TARGET_CHANGED", "修订新图只能审查原精确镜头与分镜", 409)
