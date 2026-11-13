"""Private local image objects; no client-controlled keys or public file serving."""

import hashlib
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)


def object_key(project_id: UUID, version_id: UUID, settings: Settings | None = None) -> str:
    suffix = f"{project_id}/{version_id}.image"
    if settings is not None and settings.object_storage_backend == "s3":
        return f"s3/{settings.s3_bucket}/{suffix}"
    return suffix


def object_path(root: Path | None, project_id: UUID, version_id: UUID) -> Path:
    if root is None:
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "尚未配置私有图片目录，请联系管理员", 503)
    base = root.resolve()
    path = base / object_key(project_id, version_id)
    if not path.resolve().is_relative_to(base):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "私有图片目录不可用", 503)
    return path


def write_object(
    db: Session,
    root: Path | Settings | None,
    project_id: UUID,
    version_id: UUID,
    data: bytes,
    media_type: str = "application/octet-stream",
) -> None:
    if isinstance(root, Settings):
        if root.object_storage_backend == "s3":
            store = ObjectStorage(root)
            key = object_key(project_id, version_id, root)
            store.put(key, data, media_type)

            def cleanup_s3(_session: Session) -> None:
                try:
                    store.delete(key)
                except DomainError:
                    logger.warning("Could not remove rolled-back asset object %s", version_id)

            event.listen(db, "after_rollback", cleanup_s3, once=True)
            return
        root = root.asset_storage_path
    path = object_path(root, project_id, version_id)
    created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            created = True
            stream.write(data)

        def rollback_cleanup(_session: Session) -> None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove rolled-back asset object %s", version_id)

        event.listen(db, "after_rollback", rollback_cleanup, once=True)
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "图片保存失败，请重试", 503) from exc


def read_object(
    root: Path | Settings | None, project_id: UUID, version_id: UUID, key: str, checksum: str
) -> bytes:
    if isinstance(root, Settings):
        if key.startswith("s3/"):
            if (
                not root.s3_endpoint_url
                or not root.s3_access_key.get_secret_value()
                or not root.s3_secret_key.get_secret_value()
            ):
                raise DomainError("ASSET_STORAGE_UNAVAILABLE", "尚未配置历史 S3 对象读取", 503)
            parts = key.split("/")
            if len(parts) != 4 or "/".join(parts[2:]) != object_key(project_id, version_id):
                raise DomainError("ASSET_OBJECT_UNAVAILABLE", "图片对象引用无效", 409)
            configuration = root.model_copy(
                update={
                    "object_storage_backend": "s3",
                    "s3_bucket": parts[1],
                }
            )
            try:
                return ObjectStorage(configuration).read(key, None, checksum)
            except DomainError as exc:
                raise DomainError(
                    "ASSET_OBJECT_UNAVAILABLE", "图片缺失或完整性校验失败", 409
                ) from exc
        root = root.asset_storage_path
    if key != object_key(project_id, version_id):
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "此历史对象尚未接入本机读取", 409)
    path = object_path(root, project_id, version_id)
    try:
        with path.open("rb") as stream:
            data = stream.read()
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError("Checksum mismatch")
    except (OSError, ValueError) as exc:
        raise DomainError(
            "ASSET_OBJECT_UNAVAILABLE", "图片缺失或完整性校验失败，请联系管理员", 409
        ) from exc
    return data
