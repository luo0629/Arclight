"""One bounded private-object path for imported and manually saved story originals."""

import logging
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.story_import import MAX_SOURCE_BYTES
from arclight.persistence.models import Episode, StorySource
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)


def source_key(project_id: UUID, source_id: UUID) -> str:
    return f"sources/{project_id}/{source_id}"


def write_source_object(
    db: Session, settings: Settings, project_id: UUID, source: StorySource, data: bytes
) -> None:
    store = ObjectStorage(settings)
    source.storage_backend = settings.object_storage_backend.upper()
    source.bucket = settings.s3_bucket if source.storage_backend == "S3" else None
    source.object_key = source_key(project_id, source.id)
    store.put(source.object_key, data, source.media_type)

    def rollback(_session: Session) -> None:
        try:
            store.delete(source.object_key)
        except DomainError:
            logger.warning("Could not remove rolled-back source object %s", source.id)

    event.listen(db, "after_rollback", rollback, once=True)


def source_original(db: Session, settings: Settings, source: StorySource) -> bytes:
    episode = db.get(Episode, source.episode_id)
    if episode is None or source.object_key != source_key(episode.project_id, source.id):
        raise DomainError("SOURCE_OBJECT_UNAVAILABLE", "原稿对象引用无效", 409)
    if source.storage_backend not in {"LOCAL", "S3"} or (
        source.storage_backend == "S3"
        and (
            not source.bucket
            or not settings.s3_endpoint_url
            or not settings.s3_access_key.get_secret_value()
            or not settings.s3_secret_key.get_secret_value()
        )
    ):
        raise DomainError("SOURCE_OBJECT_UNAVAILABLE", "尚未配置原稿所在的私有存储", 409)
    configuration = settings.model_copy(
        update={
            "object_storage_backend": source.storage_backend.lower(),
            "s3_bucket": source.bucket or settings.s3_bucket,
        }
    )
    try:
        data = ObjectStorage(configuration).read(source.object_key, MAX_SOURCE_BYTES, source.sha256)
        if len(data) != source.byte_size:
            raise ValueError("Source size changed")
        return data
    except (DomainError, ValueError):
        raise DomainError("SOURCE_OBJECT_UNAVAILABLE", "原稿缺失或完整性校验失败", 409) from None


def source_text(db: Session, settings: Settings, source: StorySource) -> str:
    try:
        return source_original(db, settings, source).decode(source.encoding)
    except (LookupError, UnicodeError):
        raise DomainError("SOURCE_OBJECT_UNAVAILABLE", "原稿编码校验失败", 409) from None
