"""Project authorization precedes every short-lived storage capability."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic import Field, field_validator

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.files import find_file
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.core.errors import DomainError
from arclight.persistence.models import UploadIntent
from arclight.storage.transfers import MAX_TRANSFER_SECONDS, presign_download, presign_upload

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
    byte_size: int = Field(strict=True, gt=0, le=32 * 1024 * 1024)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("original_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if value in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("文件名无效")
        if "/" in value or "\\" in value:
            raise ValueError("文件名不能包含路径")
        return value


@router.post("/uploads", status_code=201)
def create_upload(
    project_id: UUID,
    payload: UploadCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Actor,
) -> dict:
    require_project(db, project_id, actor, write=True)
    settings = request.app.state.settings
    upload_id = uuid4()
    key = f"temporary/{project_id}/{upload_id}"
    signed = presign_upload(
        settings, key, payload.media_type, payload.byte_size, payload.checksum, payload.expires_in
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=payload.expires_in)
    db.add(
        UploadIntent(
            id=upload_id,
            project_id=project_id,
            bucket=settings.s3_bucket,
            object_key=key,
            purpose=payload.purpose,
            original_name=payload.original_name,
            declared_media_type=payload.media_type,
            byte_size=payload.byte_size,
            checksum=payload.checksum,
            expires_at=expires_at,
            created_by=actor,
        )
    )
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
        "url": signed["url"],
        "fields": signed["fields"],
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
    require_project(db, project_id, actor)
    row = find_file(db, project_id, file_id)
    if row.storage_backend != "S3" or row.bucket is None:
        raise DomainError("PRESIGNED_UNAVAILABLE", "此历史本机文件请使用鉴权原图接口读取", 409)
    url = presign_download(
        request.app.state.settings,
        row.bucket,
        row.object_key,
        row.original_name,
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
        "expires_at": datetime.now(UTC) + timedelta(seconds=payload.expires_in),
        "checksum": row.checksum,
        "byte_size": row.byte_size,
    }
