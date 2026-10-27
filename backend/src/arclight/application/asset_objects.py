"""Private local image objects; no client-controlled keys or public file serving."""

import hashlib
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError

logger = logging.getLogger(__name__)


def object_key(project_id: UUID, version_id: UUID) -> str:
    return f"{project_id}/{version_id}.image"


def object_path(root: Path | None, project_id: UUID, version_id: UUID) -> Path:
    if root is None:
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "尚未配置私有图片目录，请联系管理员", 503)
    base = root.resolve()
    path = base / object_key(project_id, version_id)
    if not path.resolve().is_relative_to(base):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "私有图片目录不可用", 503)
    return path


def write_object(
    db: Session, root: Path | None, project_id: UUID, version_id: UUID, data: bytes
) -> None:
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
    root: Path | None, project_id: UUID, version_id: UUID, key: str, checksum: str
) -> bytes:
    if key != object_key(project_id, version_id):
        raise DomainError("ASSET_OBJECT_UNAVAILABLE", "此历史对象尚未接入本机读取", 409)
    path = object_path(root, project_id, version_id)
    try:
        # Do not allocate an unbounded buffer if an object was replaced outside the app.
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError("Checksum mismatch")
    except (OSError, ValueError) as exc:
        raise DomainError(
            "ASSET_OBJECT_UNAVAILABLE", "图片缺失或完整性校验失败，请联系管理员", 409
        ) from exc
    return data
