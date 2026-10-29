"""Temporary capabilities never directly register an asset version."""

import base64
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, field_validator
from sqlalchemy import select

from arclight.api.asset_versions import VersionCreate, VersionRead, create_version
from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.files import FileRead, find_file
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.application.uploads import (
    COMPLETION_GRACE,
    find_upload,
    freeze_upload,
    validated_image,
)
from arclight.core.errors import DomainError
from arclight.persistence.models import FileRegistration, UploadCompletion

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["files"])


class UploadComplete(Input):
    source_note: str = Field(min_length=1, max_length=1000)
    rights_note: str = Field(min_length=1, max_length=1000)
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_rights(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认文件使用权")
        return value


class FileRegister(Input):
    asset_id: UUID
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)


@router.get("/uploads/{upload_id}")
def get_upload(project_id: UUID, upload_id: UUID, db: Database, actor: Actor) -> dict:
    require_project(db, project_id, actor)
    row = find_upload(db, project_id, upload_id)
    result = db.scalar(select(UploadCompletion).where(UploadCompletion.upload_id == row.id))
    expired = row.expires_at.replace(tzinfo=UTC) + COMPLETION_GRACE <= datetime.now(UTC)
    return {
        "upload_id": row.id,
        "stage": "VALIDATED" if result else "EXPIRED" if expired else "TEMPORARY",
        "file_id": result.file_id if result else None,
        "expires_at": row.expires_at,
    }


@router.post("/uploads/{upload_id}/complete")
def complete_upload(
    project_id: UUID,
    upload_id: UUID,
    payload: UploadComplete,
    request: Request,
    db: Database,
    actor: Actor,
) -> FileRead:
    require_project(db, project_id, actor, write=True)
    row = find_upload(db, project_id, upload_id)
    previous = db.scalar(select(UploadCompletion).where(UploadCompletion.upload_id == row.id))
    result = freeze_upload(db, request.app.state.settings, row, actor, payload.model_dump())
    if previous is None:
        audit(db, project_id, actor, "file.upload_validated", result.id, {"upload_id": str(row.id)})
    return FileRead.model_validate(result)


@router.post("/files/{file_id}/asset-versions", status_code=201)
def register_file(
    project_id: UUID,
    file_id: UUID,
    payload: FileRegister,
    request: Request,
    db: Database,
    actor: Actor,
) -> VersionRead:
    require_project(db, project_id, actor, write=True)
    row = find_file(db, project_id, file_id)
    image = validated_image(request.app.state.settings, row)
    result = create_version(
        project_id,
        payload.asset_id,
        VersionCreate(
            creation_key=payload.creation_key,
            expected_revision=payload.expected_revision,
            original_name=row.original_name,
            media_type=cast(Literal["image/png", "image/jpeg"], image.media_type),
            content_base64=base64.b64encode(image.data).decode(),
            source_note=row.source_note,
            rights_note=row.rights_note,
            rights_confirmed=True,
        ),
        request,
        db,
        actor,
    )
    existing = db.scalar(
        select(FileRegistration).where(FileRegistration.asset_version_id == result.id)
    )
    if existing is not None:
        if existing.file_id != row.id:
            raise DomainError("IDEMPOTENCY_CONFLICT", "该版本已关联另一候选文件", 409)
    else:
        db.add(FileRegistration(file_id=row.id, asset_version_id=result.id, created_by=actor))
        db.flush()
        audit(
            db,
            project_id,
            actor,
            "file.asset_registered",
            result.id,
            {"source_file_id": str(row.id)},
        )
    return result
