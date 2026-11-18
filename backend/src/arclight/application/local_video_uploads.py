"""Validate local MP4 bytes before issuing the shared private-file receipt."""

import base64
import binascii
import logging
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import lock_dependency_graph
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.media.validation import validate_file
from arclight.persistence.models import StoredFile, User
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)


def freeze_local_video(
    db: Session,
    settings: Settings,
    project_id: UUID,
    actor: str,
    *,
    original_name: str,
    byte_size: int,
    checksum: str,
    content_base64: str,
    declaration: dict,
) -> StoredFile:
    if settings.object_storage_backend != "local":
        raise DomainError("LOCAL_UPLOAD_UNAVAILABLE", "当前存储请使用私有 S3 上传流程", 409)
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise DomainError("INVALID_FILE", "视频内容必须是有效的 Base64", 422) from None
    if len(data) != byte_size:
        raise DomainError("INVALID_UPLOAD", "上传大小与声明不一致", 422)
    video = validate_file(data, "VIDEO", original_name, "video/mp4", checksum)

    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    if actor != "service-owner":
        user = db.get(User, UUID(actor), populate_existing=True)
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "当前账号没有此操作权限", 403)

    file_id = uuid4()
    key = f"validated/{project_id}/{file_id}"
    store = ObjectStorage(settings)
    committed = False

    def cleanup(_session: Session | None = None) -> None:
        if committed:
            return
        try:
            store.delete(key)
        except DomainError:
            logger.warning("Could not remove rolled-back local video %s", file_id)

    try:
        store.put(key, video.data, video.media_type)
    except DomainError as exc:
        # A failed write can leave partial bytes, but an existing immutable object
        # belongs to its earlier transaction and must never be removed.
        if exc.code != "OBJECT_EXISTS":
            cleanup()
        raise

    def preserve_committed(_session: Session) -> None:
        nonlocal committed
        committed = True

    event.listen(db, "after_rollback", cleanup, once=True)
    event.listen(db, "after_commit", preserve_committed, once=True)
    receipt = StoredFile(
        id=file_id,
        project_id=project_id,
        asset_version_id=None,
        storage_backend="LOCAL",
        bucket=None,
        object_key=key,
        checksum=video.checksum,
        byte_size=len(video.data),
        media_type=video.media_type,
        original_name=original_name,
        created_by=actor,
        **declaration,
    )
    db.add(receipt)
    db.flush()
    db.refresh(receipt)
    audit(db, project_id, actor, "file.upload_validated", file_id, {"source": "LOCAL"})
    return receipt
