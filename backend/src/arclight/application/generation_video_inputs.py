"""Freeze exact, authorized first frames and load their bytes only for submission."""

from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_concept_assets import approved_concept_reference
from arclight.application.asset_objects import object_key
from arclight.application.commands import require_project
from arclight.application.context_sources import image_reference
from arclight.application.dependencies import exact_version
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.dependencies import IMAGE_KINDS
from arclight.domain.versions import digest
from arclight.media.validation import probe
from arclight.persistence.models import AssetVersion, GenerationJob, StoredFile
from arclight.providers.media_submission import MediaImageInput
from arclight.storage.objects import ObjectStorage

MAX_IMAGE_BYTES = 30 * 1024 * 1024


def invalid(message: str = "视频首帧与精确输入不一致", *, status: int = 409) -> NoReturn:
    raise DomainError("VIDEO_INPUT_INVALID", message, status)


def reference_inputs(inputs: list[dict]) -> list[dict]:
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        invalid()
    if any(
        item.get("asset_type") in IMAGE_KINDS
        and item.get("dependency_type") != "REFERENCE_DEPENDENCY"
        for item in inputs
    ):
        invalid("视频图像输入须绑定精确参考依赖")
    references = [item for item in inputs if item.get("dependency_type") == "REFERENCE_DEPENDENCY"]
    if len(references) > 1:
        raise DomainError(
            "PROVIDER_REFERENCES_UNSUPPORTED", "视频首帧当前仅支持一个精确参考图版本", 422
        )
    return references


def resolve_reference(
    db: Session, project_id: UUID, episode_id: UUID | None, item: dict
) -> tuple[dict, AssetVersion, StoredFile]:
    try:
        version_id = UUID(item["source_asset_version_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        invalid()
    version, asset = exact_version(db, project_id, version_id)
    if (
        asset.asset_type not in IMAGE_KINDS
        or item.get("asset_id") != str(asset.id)
        or item.get("asset_type") != asset.asset_type
        or item.get("version_no") != version.version_no
        or item.get("checksum") != version.checksum
    ):
        invalid()
    if version.stale or version.status not in {"APPROVED", "LOCKED"} or asset.status == "ARCHIVED":
        raise DomainError("ASSET_NOT_USABLE", "视频首帧必须已批准且有效", 409)
    scope = asset.metadata_json.get("usage_scope", {"kind": "PROJECT"})
    if not isinstance(scope, dict) or (
        scope.get("kind", "PROJECT") != "PROJECT"
        and not isinstance(scope.get("episode_ids", []), list)
    ):
        invalid()
    if scope.get("kind", "PROJECT") != "PROJECT" and (
        episode_id is None or str(episode_id) not in scope.get("episode_ids", [])
    ):
        raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "视频首帧不在当前剧集使用范围", 403)
    stored = db.scalar(
        select(StoredFile)
        .where(StoredFile.asset_version_id == version.id)
        .execution_options(populate_existing=True)
    )
    try:
        reference = image_reference(db, project_id, version.id)
    except DomainError as error:
        if error.code != "CONTEXT_REFERENCE_INVALID":
            raise
        invalid("视频首帧文件登记与精确版本不一致", status=422)
    if reference is None or stored is None:
        invalid("视频参考必须是已登记的精确 PNG 或 JPEG 原图", status=422)
    media = version.metadata_json.get("media", {})
    if not isinstance(media, dict):
        invalid()
    if not (
        stored.rights_confirmed
        and media.get("rights_confirmed") is True
        or approved_concept_reference(db, version)
    ):
        raise DomainError("VIDEO_INPUT_RIGHTS_UNCONFIRMED", "视频首帧使用权尚未确认", 409)
    if (
        type(reference["byte_size"]) is not int
        or not 0 < reference["byte_size"] < MAX_IMAGE_BYTES
        or media.get("media_type") != reference["media_type"]
        or media.get("byte_size") != reference["byte_size"]
        or stored.object_key != version.object_key
        or stored.storage_backend not in {"LOCAL", "S3"}
    ):
        invalid("视频首帧文件登记与版本不一致或超出大小限制", status=422)
    expected_key = object_key(project_id, version.id)
    if stored.storage_backend == "S3":
        expected_key = f"s3/{stored.bucket}/{expected_key}"
    if version.object_key != expected_key:
        invalid("视频首帧不属于当前精确版本的私有对象")
    return (
        {
            **reference,
            "role": "first_frame",
            "metadata_sha256": digest(version.metadata_json),
            "scope_sha256": digest(scope),
        },
        version,
        stored,
    )


def read_image(
    db: Session, reference: dict, version: AssetVersion, stored: StoredFile
) -> MediaImageInput:
    settings = db.info.get("settings")
    if not isinstance(settings, Settings):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "尚未配置视频首帧私有读取", 503)
    if stored.storage_backend == "S3" and (
        not settings.s3_endpoint_url
        or not settings.s3_access_key.get_secret_value()
        or not settings.s3_secret_key.get_secret_value()
    ):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "尚未配置历史 S3 对象读取", 503)
    configuration = settings.model_copy(
        update={
            "object_storage_backend": stored.storage_backend.lower(),
            "s3_bucket": stored.bucket or settings.s3_bucket,
        }
    )
    try:
        data = ObjectStorage(configuration).read(
            stored.object_key, MAX_IMAGE_BYTES - 1, reference["checksum"]
        )
        if len(data) != reference["byte_size"]:
            invalid("视频首帧长度与冻结文件不一致", status=422)
        details = probe(data, reference["media_type"])
    except DomainError as error:
        if error.code not in {"OBJECT_UNAVAILABLE", "INVALID_OBJECT_KEY", "INVALID_FILE"}:
            raise
        invalid("视频首帧缺失、完整性校验失败或不符合安全解析限制", status=422)
    width, height = details.get("width"), details.get("height")
    if (
        type(width) is not int
        or type(height) is not int
        or not 300 <= width <= 6000
        or not 300 <= height <= 6000
        or not 0.4 <= width / height <= 2.5
        or version.metadata_json["media"].get("width") != width
        or version.metadata_json["media"].get("height") != height
    ):
        invalid("视频首帧尺寸或画幅比例不符合模型要求", status=422)
    # probe also retains the existing stricter 4096-edge / 4MP image sandbox limits.
    return MediaImageInput(role="first_frame", media_type=reference["media_type"], data=data)


def freeze_video_images(
    db: Session,
    project_id: UUID,
    actor: str,
    episode_id: UUID | None,
    inputs: list[dict],
) -> list[dict]:
    references = reference_inputs(inputs)
    if not references:
        return []
    require_project(db, project_id, actor, write=True)
    reference, version, stored = resolve_reference(db, project_id, episode_id, references[0])
    read_image(db, reference, version, stored)
    return [reference]


def checked_references(
    db: Session, job: GenerationJob, snapshot: dict
) -> list[tuple[dict, AssetVersion, StoredFile]]:
    try:
        context, inputs = snapshot["context"], snapshot["inputs"]
        frozen = context.get("video_images", [])
    except (KeyError, TypeError, AttributeError):
        invalid()
    if not isinstance(frozen, list):
        invalid()
    if job.task_type != "VIDEO":
        if frozen:
            invalid()
        return []
    references = reference_inputs(inputs)
    if len(frozen) != len(references):
        invalid("视频首帧清单与参考依赖不一致")
    if not references:
        return []
    require_project(db, job.project_id, job.created_by, write=True)
    current, version, stored = resolve_reference(db, job.project_id, job.episode_id, references[0])
    if frozen != [current]:
        invalid("视频首帧元数据、作用范围或文件身份已变化")
    return [(current, version, stored)]


def validate_video_images(db: Session, job: GenerationJob, snapshot: dict) -> None:
    """Check current authorization and frozen identities without decoding bytes again."""
    checked_references(db, job, snapshot)


def load_video_images(
    db: Session, job: GenerationJob, snapshot: dict
) -> tuple[MediaImageInput, ...]:
    """Return ephemeral bytes before an attempt or provider call can be recorded."""
    return tuple(
        read_image(db, reference, version, stored)
        for reference, version, stored in checked_references(db, job, snapshot)
    )
