"""Project-scoped candidate history and explicit upload-backed replacements."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.asset_versions import VersionRead, read_version
from arclight.api.dependencies import Actor, Database
from arclight.api.files import find_file
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.asset_objects import read_object
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_candidates import append_candidate_version
from arclight.application.generation_state import find_job
from arclight.application.production_versions import digest
from arclight.application.uploads import validated_image
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetVersion,
    FileRegistration,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    VisualAsset,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["generation-candidates"])


class CandidateRead(Resource):
    job_id: UUID
    attempt_id: UUID
    input_manifest_id: UUID
    ordinal: int | None
    source: str
    asset_version_id: UUID | None
    source_file_id: UUID | None
    replaces_candidate_id: UUID | None
    error_code: str | None
    created_by: str
    required_review: str = "CONTINUITY"
    version: VersionRead | None = None
    asset_revision: int | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CandidateReplace(Input):
    file_id: UUID
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)


def find_candidate(db: Database, project_id: UUID, candidate_id: UUID) -> GenerationCandidate:
    row = db.get(GenerationCandidate, candidate_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "生成候选不存在", 404)
    return row


def read_candidate(db: Database, row: GenerationCandidate) -> CandidateRead:
    result = CandidateRead.model_validate(row)
    version = db.get(AssetVersion, row.asset_version_id) if row.asset_version_id else None
    if version is not None:
        result.version = read_version(version)
        result.version.created_at = CandidateRead.utc_timestamp(result.version.created_at)
        result.version.updated_at = CandidateRead.utc_timestamp(result.version.updated_at)
        asset = db.get(VisualAsset, version.asset_id)
        result.asset_revision = asset.revision if asset else None
    return result


@router.get("/generation-jobs/{job_id}/candidates")
def list_candidates(
    project_id: UUID,
    job_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[CandidateRead]:
    require_project(db, project_id, actor)
    job = find_job(db, job_id)
    if job.project_id != project_id:
        raise DomainError("NOT_FOUND", "媒体任务不存在", 404)
    return [
        read_candidate(db, row)
        for row in db.scalars(
            select(GenerationCandidate)
            .where(GenerationCandidate.job_id == job_id)
            .order_by(GenerationCandidate.created_at, GenerationCandidate.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/generation-candidates/{candidate_id}/image")
def candidate_image(
    project_id: UUID, candidate_id: UUID, request: Request, db: Database, actor: Actor
) -> Response:
    require_project(db, project_id, actor)
    row = find_candidate(db, project_id, candidate_id)
    version = db.get(AssetVersion, row.asset_version_id) if row.asset_version_id else None
    if version is None:
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "候选未通过媒体检查", 409)
    data = read_object(
        request.app.state.settings, project_id, version.id, version.object_key, version.checksum
    )
    return Response(
        data,
        media_type=version.metadata_json["media"]["media_type"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="candidate-image"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.post("/generation-candidates/{candidate_id}/replacements", status_code=201)
def replace_candidate(
    project_id: UUID,
    candidate_id: UUID,
    payload: CandidateReplace,
    request: Request,
    db: Database,
    actor: Actor,
) -> CandidateRead:
    require_project(db, project_id, actor, write=True)
    source = find_candidate(db, project_id, candidate_id)
    fingerprint = digest({"candidate_id": str(source.id), "file_id": str(payload.file_id)})
    existing = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.creation_key == payload.creation_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "替换标识不能关联不同图片或候选", 409)
        return read_candidate(db, existing)
    job = db.get(GenerationJob, source.job_id)
    attempt = db.get(GenerationAttempt, source.attempt_id)
    assert job is not None and attempt is not None
    version = db.get(AssetVersion, source.asset_version_id) if source.asset_version_id else None
    asset_id = (
        version.asset_id if version else (job.target_asset_id or uuid5(job.id, "candidate-asset"))
    )
    asset = db.get(VisualAsset, asset_id, populate_existing=True) if asset_id else None
    if asset is not None and asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "已归档或锁定资产不能替换", 409)
    if (asset.revision if asset else 0) != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "候选资产已变化，请刷新后重试", 409)
    file = find_file(db, project_id, payload.file_id)
    image = validated_image(request.app.state.settings, file)
    output = append_candidate_version(
        db,
        request.app.state.settings,
        job,
        attempt,
        image,
        payload.creation_key,
        actor,
        media_notes={
            "original_name": file.original_name,
            "source_note": file.source_note,
            "rights_note": file.rights_note,
            "rights_confirmed": file.rights_confirmed,
        },
        replaces_version_id=source.asset_version_id,
    )
    row = GenerationCandidate(
        project_id=project_id,
        job_id=job.id,
        attempt_id=attempt.id,
        input_manifest_id=source.input_manifest_id,
        source="REPLACEMENT",
        asset_version_id=output.id,
        source_file_id=file.id,
        replaces_candidate_id=source.id,
        creation_key=payload.creation_key,
        request_fingerprint=fingerprint,
        created_by=actor,
    )
    db.add(row)
    db.add(FileRegistration(file_id=file.id, asset_version_id=output.id, created_by=actor))
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "generation.candidate_replaced",
        row.id,
        {
            "replaces_candidate_id": str(source.id),
            "asset_version_id": str(output.id),
            "source_file_id": str(file.id),
        },
    )
    return read_candidate(db, row)
