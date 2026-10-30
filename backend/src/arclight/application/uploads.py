"""Read, validate and freeze uploaded bytes before exposing a file receipt."""

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import MAX_IMAGE_BYTES, ImportedImage, decode_image
from arclight.domain.file_policy import check_file_policy
from arclight.media.validation import validate_file
from arclight.persistence.models import StoredFile, UploadCompletion, UploadIntent
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)
COMPLETION_GRACE = timedelta(hours=24)


def find_upload(db: Session, project_id: UUID, upload_id: UUID) -> UploadIntent:
    row = db.get(UploadIntent, upload_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "上传记录不存在", 404)
    return row


def file_store(settings: Settings, bucket: str) -> ObjectStorage:
    if (
        not settings.s3_endpoint_url
        or not settings.s3_access_key.get_secret_value()
        or not settings.s3_secret_key.get_secret_value()
    ):
        raise DomainError("STORAGE_UNAVAILABLE", "尚未配置文件所在的私有 S3", 503)
    return ObjectStorage(
        settings.model_copy(update={"object_storage_backend": "s3", "s3_bucket": bucket})
    )


def validated_image(settings: Settings, row: StoredFile) -> ImportedImage:
    if (
        row.storage_backend != "S3"
        or row.bucket is None
        or row.asset_version_id is not None
        or row.media_type not in {"image/png", "image/jpeg"}
    ):
        raise DomainError("INVALID_CANDIDATE", "请选择已验证的候选图片文件", 409)
    data = file_store(settings, row.bucket).read(row.object_key, MAX_IMAGE_BYTES, row.checksum)
    return decode_image(base64.b64encode(data).decode(), row.media_type)


def freeze_upload(
    db: Session, settings: Settings, row: UploadIntent, actor: str, declaration: dict
) -> StoredFile:
    fingerprint = hashlib.sha256(
        json.dumps(declaration, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    existing = db.scalar(select(UploadCompletion).where(UploadCompletion.upload_id == row.id))
    if existing is not None:
        if existing.request_sha256 != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "已完成的上传不能修改来源与授权", 409)
        result = db.get(StoredFile, existing.file_id)
        assert result is not None
        return result
    if row.expires_at.replace(tzinfo=UTC) + COMPLETION_GRACE <= datetime.now(UTC):
        raise DomainError("UPLOAD_EXPIRED", "上传已过期，请重新上传", 409)
    policy = check_file_policy(
        row.purpose, row.original_name, row.declared_media_type, row.byte_size
    )
    store = file_store(settings, row.bucket)
    data = store.read(row.object_key, policy.max_bytes, row.checksum)
    if len(data) != row.byte_size:
        raise DomainError("INVALID_UPLOAD", "上传大小与声明不一致", 422)
    image = validate_file(
        data, row.purpose, row.original_name, row.declared_media_type, row.checksum
    )
    file_id = uuid4()
    key = f"validated/{row.project_id}/{file_id}"
    store.put(key, image.data, image.media_type)

    def cleanup(_session: Session) -> None:
        try:
            store.delete(key)
        except DomainError:
            logger.warning("Could not remove rolled-back validated file %s", file_id)

    event.listen(db, "after_rollback", cleanup, once=True)
    receipt = StoredFile(
        id=file_id,
        project_id=row.project_id,
        asset_version_id=None,
        storage_backend="S3",
        bucket=row.bucket,
        object_key=key,
        checksum=image.checksum,
        byte_size=len(image.data),
        media_type=image.media_type,
        original_name=row.original_name,
        created_by=actor,
        **declaration,
    )
    db.add(receipt)
    db.flush()
    db.refresh(receipt)
    db.add(
        UploadCompletion(
            upload_id=row.id, file_id=file_id, purpose=row.purpose, request_sha256=fingerprint
        )
    )
    db.flush()
    return receipt
