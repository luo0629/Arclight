"""Bind existing STORE validation and dependency checks to production candidates."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.application.dependencies import require_manifest, validate_manifest_inputs
from arclight.application.production_versions import digest
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import ImportedImage
from arclight.domain.candidate_checks import CandidateMediaCheck, CheckedImage
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    VisualAsset,
)


def check_candidate_media(
    db: Session,
    job: GenerationJob,
    attempt: GenerationAttempt,
    candidate_id: UUID,
    image: ImportedImage | None,
    error_code: str | None = None,
) -> CandidateMediaCheck:
    """Consume decoded STORE evidence; do not introduce another file parser."""
    manifest = require_manifest(db, job.project_id, job.input_manifest_id)
    references_valid = False
    try:
        if attempt.job_id != job.id or attempt.input_manifest_id != manifest.id:
            raise DomainError("INVALID_DEPENDENCY_INPUT", "候选与原始生产输入不匹配", 409)
        validate_manifest_inputs(db, job.project_id, manifest, job.target_type)
        references_valid = True
    except DomainError as exc:
        error_code = error_code or exc.code
    if image is None:
        error_code = error_code or "INVALID_IMAGE"
    return CandidateMediaCheck(
        candidate_id=candidate_id,
        input_manifest_id=manifest.id,
        manifest_sha256=digest(manifest.snapshot_json),
        status="FAILED" if error_code else "PASSED",
        references_valid=references_valid,
        image=CheckedImage(
            checksum=image.checksum,
            media_type=image.media_type,
            byte_size=len(image.data),
            width=image.width,
            height=image.height,
        )
        if image is not None
        else None,
        error_code=error_code,
    )


def require_candidate_media_check(
    db: Session, project_id: UUID, candidate_id: UUID
) -> CandidateMediaCheck:
    """The shared admission point for downstream review of an exact candidate."""
    row = db.get(GenerationCandidate, candidate_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "生成候选不存在", 404)
    try:
        check = CandidateMediaCheck.model_validate(row.media_check_json)
    except ValidationError as exc:
        raise DomainError("MEDIA_CHECK_REQUIRED", "候选缺少有效的媒体检查记录", 409) from exc
    version = db.get(AssetVersion, row.asset_version_id) if row.asset_version_id else None
    asset = db.get(VisualAsset, version.asset_id) if version else None
    manifest = require_manifest(db, project_id, row.input_manifest_id)
    job = db.get(GenerationJob, row.job_id)
    attempt = db.get(GenerationAttempt, row.attempt_id)
    if (
        check.status != "PASSED"
        or row.error_code is not None
        or job is None
        or job.project_id != project_id
        or job.input_manifest_id != manifest.id
        or attempt is None
        or attempt.job_id != job.id
        or attempt.input_manifest_id != manifest.id
        or check.candidate_id != row.id
        or check.input_manifest_id != manifest.id
        or check.manifest_sha256 != digest(manifest.snapshot_json)
        or version is None
        or asset is None
        or asset.project_id != project_id
        or version.metadata_json.get("generation_job_id") != str(row.job_id)
        or version.metadata_json.get("generation_attempt_id") != str(row.attempt_id)
        or version.metadata_json.get("dependency_manifest_id") != str(manifest.id)
        or check.image is None
        or check.image.checksum != version.checksum
        or any(
            version.metadata_json.get("media", {}).get(key) != value
            for key, value in check.image.model_dump().items()
            if key != "checksum"
        )
    ):
        raise DomainError("MEDIA_CHECK_REQUIRED", "候选未通过精确版本的媒体检查", 409)
    validate_manifest_inputs(db, project_id, manifest, asset.asset_type)
    return check
