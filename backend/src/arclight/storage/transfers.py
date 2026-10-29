"""Short-lived S3 capabilities, constrained to an exact server-owned object."""

from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.storage.objects import ObjectStorage, safe_key

MAX_TRANSFER_SECONDS = 300


def signing_store(settings: Settings, bucket: str) -> ObjectStorage:
    if (
        not settings.s3_endpoint_url
        or not settings.s3_access_key.get_secret_value()
        or not settings.s3_secret_key.get_secret_value()
    ):
        raise DomainError("PRESIGNED_UNAVAILABLE", "预签名传输需要配置私有 S3 存储", 503)
    return ObjectStorage(
        settings.model_copy(
            update={
                "object_storage_backend": "s3",
                "s3_bucket": bucket,
                "s3_endpoint_url": settings.s3_public_endpoint_url or settings.s3_endpoint_url,
            }
        )
    )


def presign_upload(
    settings: Settings, key: str, media_type: str, byte_size: int, checksum: str, seconds: int
) -> dict:
    safe_key(key)
    if settings.object_storage_backend != "s3":
        raise DomainError("PRESIGNED_UNAVAILABLE", "预签名上传需要启用私有 S3 存储", 503)
    fields = {"Content-Type": media_type, "x-amz-meta-sha256": checksum}
    try:
        store = signing_store(settings, settings.s3_bucket)
        assert store.client is not None
        return store.client.generate_presigned_post(
            Bucket=settings.s3_bucket,
            Key=key,
            Fields=fields,
            Conditions=[
                *({name: value} for name, value in fields.items()),
                ["content-length-range", byte_size, byte_size],
            ],
            ExpiresIn=min(seconds, MAX_TRANSFER_SECONDS),
        )
    except (BotoCoreError, ClientError):
        raise DomainError("STORAGE_UNAVAILABLE", "上传地址签发失败，请重试", 503) from None


def presign_download(
    settings: Settings, bucket: str, key: str, original_name: str, seconds: int
) -> str:
    safe_key(key)
    try:
        store = signing_store(settings, bucket)
        assert store.client is not None
        return store.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentDisposition": "attachment; filename*=UTF-8''"
                + quote(original_name, safe=""),
                "ResponseContentType": "application/octet-stream",
                "ResponseCacheControl": "private, no-store",
            },
            ExpiresIn=min(seconds, MAX_TRANSFER_SECONDS),
            HttpMethod="GET",
        )
    except (BotoCoreError, ClientError):
        raise DomainError("STORAGE_UNAVAILABLE", "下载地址签发失败，请重试", 503) from None
