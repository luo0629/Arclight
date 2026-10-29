"""Bounded, immutable object writes; credentials never leave this adapter."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from arclight.core.config import Settings
from arclight.core.errors import DomainError


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    media_type: str
    version_id: str | None = None


def safe_key(key: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,510}", key) or any(
        part in {"", ".", ".."} for part in key.split("/")
    ):
        raise DomainError("INVALID_OBJECT_KEY", "对象引用无效", 422)
    return key


class ObjectStorage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        if settings.object_storage_backend == "s3":
            self.client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key.get_secret_value(),
                aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"total_max_attempts": 1},
                    proxies={},
                ),
            )

    def path(self, key: str) -> Path:
        root = self.settings.asset_storage_path
        if root is None:
            raise DomainError("STORAGE_UNAVAILABLE", "尚未配置对象存储", 503)
        base = root.resolve()
        path = base / safe_key(key)
        if not path.resolve().is_relative_to(base):
            raise DomainError("INVALID_OBJECT_KEY", "对象引用无效", 422)
        return path

    def check(self) -> None:
        try:
            if self.client is not None:
                self.client.head_bucket(Bucket=self.settings.s3_bucket)
            else:
                root = self.settings.asset_storage_path
                if root is None or not root.is_dir():
                    raise OSError("Missing storage directory")
        except (OSError, BotoCoreError, ClientError):
            raise DomainError("STORAGE_UNAVAILABLE", "对象存储不可用", 503) from None

    def put(self, key: str, data: bytes, media_type: str) -> None:
        safe_key(key)
        try:
            if self.client is not None:
                self.client.put_object(
                    Bucket=self.settings.s3_bucket,
                    Key=key,
                    Body=data,
                    ContentType=media_type,
                    Metadata={"sha256": hashlib.sha256(data).hexdigest()},
                    IfNoneMatch="*",
                )
            else:
                path = self.path(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(data)
        except FileExistsError:
            raise DomainError("OBJECT_EXISTS", "对象已存在，不能覆盖历史文件", 409) from None
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] in {409, 412}:
                raise DomainError("OBJECT_EXISTS", "对象已存在，不能覆盖历史文件", 409) from None
            raise DomainError("STORAGE_UNAVAILABLE", "对象保存失败，请重试", 503) from None
        except (OSError, BotoCoreError):
            raise DomainError("STORAGE_UNAVAILABLE", "对象保存失败，请重试", 503) from None

    def read(self, key: str, max_bytes: int, checksum: str | None = None) -> bytes:
        safe_key(key)
        try:
            if self.client is not None:
                response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
                with response["Body"] as stream:
                    if response["ContentLength"] > max_bytes:
                        raise ValueError("Object too large")
                    data = stream.read(max_bytes + 1)
            else:
                with self.path(key).open("rb") as stream:
                    data = stream.read(max_bytes + 1)
            if len(data) > max_bytes or (
                checksum is not None and hashlib.sha256(data).hexdigest() != checksum
            ):
                raise ValueError("Object integrity mismatch")
            return data
        except (OSError, ValueError, BotoCoreError, ClientError):
            raise DomainError("OBJECT_UNAVAILABLE", "文件缺失或完整性校验失败", 409) from None

    def stat(self, key: str) -> ObjectInfo:
        safe_key(key)
        try:
            if self.client is not None:
                result = self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
                return ObjectInfo(result["ContentLength"], result["ContentType"])
            return ObjectInfo(self.path(key).stat().st_size, "application/octet-stream")
        except (OSError, BotoCoreError, ClientError):
            raise DomainError("OBJECT_UNAVAILABLE", "文件不存在或暂不可读", 409) from None

    def delete(self, key: str) -> None:
        safe_key(key)
        try:
            if self.client is not None:
                self.client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
            else:
                self.path(key).unlink(missing_ok=True)
        except (OSError, BotoCoreError, ClientError):
            raise DomainError("STORAGE_UNAVAILABLE", "对象清理失败，请重试", 503) from None


def initialize(settings: Settings) -> None:
    store = ObjectStorage(settings)
    if store.client is None:
        if settings.asset_storage_path is None:
            raise DomainError("STORAGE_UNAVAILABLE", "尚未配置对象存储", 503)
        settings.asset_storage_path.mkdir(parents=True, exist_ok=True)
    else:
        try:
            store.client.head_bucket(Bucket=settings.s3_bucket)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise DomainError("STORAGE_UNAVAILABLE", "对象存储初始化失败", 503) from None
            options = {}
            if settings.s3_region != "us-east-1":
                options["CreateBucketConfiguration"] = {"LocationConstraint": settings.s3_region}
            store.client.create_bucket(Bucket=settings.s3_bucket, **options)
    store.check()


if __name__ == "__main__":
    from arclight.core.config import get_settings

    initialize(get_settings())
