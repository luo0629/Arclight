"""Project authorization precedes every short-lived storage capability."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator, model_validator

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.files import find_file
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.application.file_transfer_grants import issue_ticket
from arclight.core.errors import DomainError
from arclight.domain.file_policy import check_file_policy
from arclight.persistence.models import UploadIntent
from arclight.storage.transfers import MAX_TRANSFER_SECONDS

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["files"])


class TransferExpiry(Input):
    expires_in: int = Field(default=120, strict=True, ge=1, le=MAX_TRANSFER_SECONDS)


class UploadCreate(TransferExpiry):
    purpose: Literal["REFERENCE_IMAGE", "STORY", "AUDIO", "MUSIC", "FONT"]
    original_name: str = Field(min_length=1, max_length=160)
    media_type: Literal[
        "image/png",
        "image/jpeg",
        "text/plain",
        "text/markdown",
        "audio/wav",
        "audio/mpeg",
        "audio/ogg",
        "font/ttf",
        "font/otf",
    ]
    byte_size: int = Field(strict=True, gt=0)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("original_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if value in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("文件名无效")
        if "/" in value or "\\" in value:
            raise ValueError("文件名不能包含路径")
        return value

    @model_validator(mode="after")
    def allowed_file(self) -> "UploadCreate":
        check_file_policy(self.purpose, self.original_name, self.media_type, self.byte_size)
        return self


@router.post("/uploads", status_code=201)
def create_upload(
    project_id: UUID,
    payload: UploadCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> dict:
    project = require_project(db, project_id, actor, write=True)
    settings = request.app.state.settings
    if settings.object_storage_backend != "s3":
        raise DomainError("PRESIGNED_UNAVAILABLE", "文件直传需要启用私有 S3 存储", 503)
    upload_id = uuid4()
    key = f"temporary/{project_id}/{upload_id}"
    row = UploadIntent(
        id=upload_id,
        project_id=project_id,
        bucket=settings.s3_bucket,
        object_key=key,
        purpose=payload.purpose,
        original_name=payload.original_name,
        declared_media_type=payload.media_type,
        byte_size=payload.byte_size,
        checksum=payload.checksum,
        created_by=actor,
    )
    url, expires_at = issue_ticket(
        db,
        settings,
        project,
        row,
        actor,
        request.headers.get("Authorization", "").partition(" ")[2],
        payload.expires_in,
    )
    row.expires_at = expires_at
    db.add(row)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "file.upload_url_issued",
        upload_id,
        {"expires_in": payload.expires_in, "byte_size": payload.byte_size},
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "upload_id": upload_id,
        "method": "POST",
        "url": url,
        "headers": {"Content-Type": payload.media_type},
        "body": "raw",
        "expires_at": expires_at,
    }


@router.post("/files/{file_id}/download-url")
def create_download(
    project_id: UUID,
    file_id: UUID,
    payload: TransferExpiry,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> dict:
    project = require_project(db, project_id, actor)
    row = find_file(db, project_id, file_id)
    if row.storage_backend != "S3" or row.bucket is None:
        raise DomainError("PRESIGNED_UNAVAILABLE", "此历史本机文件请使用鉴权原图接口读取", 409)
    url, expires_at = issue_ticket(
        db,
        request.app.state.settings,
        project,
        row,
        actor,
        request.headers.get("Authorization", "").partition(" ")[2],
        payload.expires_in,
    )
    audit(
        db,
        project_id,
        actor,
        "file.download_url_issued",
        row.id,
        {"expires_in": payload.expires_in},
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "file_id": row.id,
        "method": "GET",
        "url": url,
        "expires_at": expires_at,
        "checksum": row.checksum,
        "byte_size": row.byte_size,
    }
