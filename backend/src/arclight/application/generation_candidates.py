"""Import each durable image result independently, outside Provider business locks."""

import base64
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.art_concept_jobs import concept_origin
from arclight.application.asset_objects import object_key, write_object
from arclight.application.audit import audit
from arclight.application.candidate_checks import (
    check_candidate_media,
    require_candidate_media_check,
)
from arclight.application.dependencies import attach_output, lock_dependency_graph
from arclight.application.files import record_asset_file
from arclight.application.production_versions import bind_output, digest
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import ImportedImage, decode_image
from arclight.domain.continuity_reviews import ReviewContractRecord
from arclight.persistence.generation_candidates import GenerationImageImport
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    Project,
    VisualAsset,
)
from arclight.providers.media_io import bounded_call
from arclight.providers.media_outputs import MediaOutputProvider
from arclight.providers.media_submission import MediaSubmission


def requires_continuity_review(version: AssetVersion, asset: VisualAsset | None) -> bool:
    gate = version.metadata_json.get("required_review")
    return (
        gate == "CONTINUITY"
        or asset is not None
        and (
            asset.asset_type == "SHOT_IMAGE"
            or asset.asset_type == "IMAGE"
            and version.creation_key is not None
            and gate != "ART_PROPOSAL"
        )
    )


def require_candidate_review(
    version: AssetVersion, db: Session | None = None
) -> ReviewContractRecord | None:
    gate = version.metadata_json.get("required_review")
    asset = db.get(VisualAsset, version.asset_id) if db is not None else None
    if requires_continuity_review(version, asset):
        if db is None:
            raise DomainError("CONTINUITY_REVIEW_REQUIRED", "候选图片须先完成必需的连续性审查", 409)
        from arclight.application.continuity_selection import require_shot_review

        return require_shot_review(db, version)
    if gate == "ART_PROPOSAL":
        from arclight.application.art_concept_assets import (
            approved_concept_reference,
            concept_candidate,
        )

        if db is not None:
            candidate = db.scalar(
                select(GenerationCandidate).where(
                    GenerationCandidate.asset_version_id == version.id
                )
            )
            # Preserve pre-QA-01 ART approvals; they still use the original exact
            # concept/rights approval checks below. Shot review never accepts NULL.
            if candidate is not None and candidate.media_check_json is not None:
                require_candidate_media_check(db, candidate.project_id, candidate.id)
        if db is None or not approved_concept_reference(db, version):
            raise DomainError(
                "ART_REFERENCE_APPROVAL_REQUIRED", "概念图须经美术提案精确人工批准及授权确认", 409
            )
        concept_candidate(db, None, version.id)
    if db is None:
        raise DomainError("CONTINUITY_REVIEW_REQUIRED", "选用门禁须读取持久化检查和审查凭据", 409)
    return None


def candidate_asset(db: Session, job: GenerationJob) -> VisualAsset:
    identity = job.target_asset_id or uuid5(job.id, "candidate-asset")
    row = db.get(VisualAsset, identity, populate_existing=True)
    if row is None:
        if job.target_asset_id is not None:
            raise DomainError("NOT_FOUND", "生成目标不存在", 404)
        row = VisualAsset(
            id=identity,
            project_id=job.project_id,
            asset_type=job.target_type,
            name=f"生成候选 {str(job.id)[:8]}",
            created_by=job.created_by,
            metadata_json={},
        )
        db.add(row)
        db.flush()
    if row.project_id != job.project_id or row.asset_type != job.target_type:
        raise DomainError("INVALID_GENERATION_TARGET", "候选资产不匹配", 409)
    return row


def append_candidate_version(
    db: Session,
    settings: Settings,
    job: GenerationJob,
    attempt: GenerationAttempt,
    image: ImportedImage,
    creation_key: UUID,
    actor: str,
    *,
    media_notes: dict,
    replaces_version_id: UUID | None = None,
) -> AssetVersion:
    asset = candidate_asset(db, job)
    prior_imports = (
        db.scalar(
            select(func.count(GenerationCandidate.id)).where(
                GenerationCandidate.job_id == job.id,
                GenerationCandidate.source == "GENERATED",
                GenerationCandidate.asset_version_id.is_not(None),
            )
        )
        or 0
    )
    changed_target = job.target_asset_id is not None and (
        asset.current_version_id != job.expected_current_version_id
        or asset.lock_version != job.expected_lock_version
        or asset.revision != (job.expected_asset_revision or 0) + prior_imports
    )
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
        )
        or 0
    ) + 1
    version = AssetVersion(
        id=uuid4(),
        asset_id=asset.id,
        version_no=number,
        checksum=image.checksum,
        creation_key=creation_key,
        created_by=actor,
        status="DRAFT",
        stale=changed_target,
        metadata_json={
            "storage_backend": settings.object_storage_backend.upper(),
            "dependency_manifest_id": str(job.input_manifest_id),
            "generation_job_id": str(job.id),
            "generation_attempt_id": str(attempt.id),
            "required_review": "ART_PROPOSAL" if concept_origin(db, job) else "CONTINUITY",
            "target_changed_since_submission": changed_target,
            "replaces_version_id": str(replaces_version_id) if replaces_version_id else None,
            "asset_snapshot": {
                "id": str(asset.id),
                "name": asset.name,
                "asset_type": asset.asset_type,
                "revision": asset.revision,
                "current_version_id": str(asset.current_version_id)
                if asset.current_version_id
                else None,
            },
            "media": {
                "media_type": image.media_type,
                "byte_size": len(image.data),
                "width": image.width,
                "height": image.height,
                **media_notes,
            },
        },
    )
    version.object_key = object_key(job.project_id, version.id, settings)
    write_object(db, settings, job.project_id, version.id, image.data, image.media_type)
    db.add(version)
    # Append history even when the target was locked/archived during the call.
    # Never change its selected version, lock state, or lifecycle.
    asset.revision += 1
    db.flush()
    record_asset_file(db, settings, job.project_id, version)
    attach_output(db, job.project_id, job.input_manifest_id, version)
    bind_output(db, job.project_id, "ATTEMPT", attempt.id, "ASSET_VERSION", version.id)
    return version


def import_attempt(
    factory: sessionmaker[Session],
    settings: Settings,
    attempt_id: UUID,
    provider: MediaOutputProvider,
) -> int:
    with factory() as db:
        attempt = db.get(GenerationAttempt, attempt_id)
        if attempt is None or attempt.status != "SUCCEEDED" or attempt.result_json is None:
            return 0
        if (
            db.scalar(
                select(GenerationImageImport.id).where(
                    GenerationImageImport.attempt_id == attempt_id
                )
            )
            is None
        ):
            return 0  # No fabricated image evidence for pre-GEN-12 result handles.
        job = db.get(GenerationJob, attempt.job_id)
        assert job is not None
        if job.task_type != "IMAGE" or provider.name != attempt.provider:
            return 0
        request = MediaSubmission(
            job.id,
            attempt.id,
            attempt.input_manifest_id,
            deepcopy(attempt.input_snapshot_json),
            None,
        )
        project_id = job.project_id
        handles = list(attempt.result_json["output_handles"])
    imported = 0
    for ordinal, handle in enumerate(handles):
        with factory() as db:
            if (
                db.scalar(
                    select(GenerationCandidate.id).where(
                        GenerationCandidate.attempt_id == attempt_id,
                        GenerationCandidate.ordinal == ordinal,
                    )
                )
                is not None
            ):
                continue
        image = None
        error = None
        try:
            output = bounded_call(partial(provider.download, request, handle, None), 30)
            if not output.data:
                raise DomainError("INVALID_IMAGE", "生成图片不能为空", 422)
            # Untrusted results use a private temporary object before validation;
            # only successfully decoded bytes reach immutable asset storage.
            with TemporaryDirectory(prefix="arclight-generation-") as folder:
                staging = Path(folder) / "candidate"
                with staging.open("xb") as stream:
                    stream.write(output.data)
                with staging.open("rb") as stream:
                    staged = stream.read()
                image = decode_image(base64.b64encode(staged).decode(), output.media_type)
        except DomainError as exc:
            if exc.status_code >= 500:
                continue  # Temporary retrieval/storage failure remains durably pending.
            error = exc.code
            image = None
        except (TimeoutError, ConnectionError, OSError):
            continue
        with factory.begin() as db:
            lock_dependency_graph(db, project_id)
            if (
                db.scalar(
                    select(GenerationCandidate.id).where(
                        GenerationCandidate.attempt_id == attempt_id,
                        GenerationCandidate.ordinal == ordinal,
                    )
                )
                is not None
            ):
                continue
            attempt = db.get(GenerationAttempt, attempt_id)
            assert attempt is not None
            job = db.get(GenerationJob, attempt.job_id)
            assert job is not None
            key = uuid5(attempt_id, f"image:{ordinal}")
            candidate_id = uuid4()
            check = check_candidate_media(db, job, attempt, candidate_id, image, error)
            error = check.error_code
            version = (
                append_candidate_version(
                    db,
                    settings,
                    job,
                    attempt,
                    image,
                    key,
                    "media-worker",
                    media_notes={
                        "original_name": f"candidate-{ordinal + 1}."
                        + ("png" if image.media_type == "image/png" else "jpg"),
                        "source_note": "Provider生成结果",
                        "rights_note": "生成来源已记录，授权按项目政策审核",
                        "rights_confirmed": False,
                    },
                )
                if image and check.status == "PASSED"
                else None
            )
            row = GenerationCandidate(
                id=candidate_id,
                media_check_json=check.model_dump(mode="json"),
                project_id=project_id,
                job_id=job.id,
                attempt_id=attempt.id,
                input_manifest_id=job.input_manifest_id,
                ordinal=ordinal,
                source="GENERATED",
                output_handle=handle,
                asset_version_id=version.id if version else None,
                creation_key=key,
                request_fingerprint=digest(
                    {"attempt": str(attempt.id), "ordinal": ordinal, "handle": handle}
                ),
                error_code=error,
                created_by="media-worker",
            )
            db.add(row)
            db.flush()
            audit(
                db,
                project_id,
                "media-worker",
                "generation.candidate_registered",
                row.id,
                {
                    "job_id": str(job.id),
                    "attempt_id": str(attempt.id),
                    "ordinal": ordinal,
                    "asset_version_id": str(version.id) if version else None,
                    "error_code": error,
                },
            )
            imported += 1
    return imported


def pending_attempts(factory: sessionmaker[Session], limit: int = 100) -> list[UUID]:
    with factory() as db:
        # Count immutable outcomes rather than trusting a broker acknowledgment.
        completed = (
            select(func.count(GenerationCandidate.id))
            .where(
                GenerationCandidate.attempt_id == GenerationImageImport.attempt_id,
                GenerationCandidate.source == "GENERATED",
            )
            .correlate(GenerationImageImport)
            .scalar_subquery()
        )
        return list(
            db.scalars(
                select(GenerationImageImport.attempt_id)
                .join(GenerationAttempt, GenerationAttempt.id == GenerationImageImport.attempt_id)
                .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
                .join(Project, Project.id == GenerationJob.project_id)
                .where(
                    GenerationAttempt.status == "SUCCEEDED",
                    Project.deleted_at.is_(None),
                    Project.archived.is_(False),
                    completed < GenerationImageImport.output_count,
                )
                .order_by(GenerationImageImport.created_at, GenerationImageImport.id)
                .limit(limit)
            )
        )
