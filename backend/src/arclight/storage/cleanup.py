"""Bounded listings and rechecked deletion for exclusively server-managed keys."""

import hashlib
import heapq
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from arclight.core.errors import DomainError
from arclight.storage.objects import ObjectStorage


@dataclass(frozen=True)
class Snapshot:
    key: str
    size: int
    modified: datetime
    token: str


def identity(store: ObjectStorage) -> str:
    settings = store.settings
    location = (
        f"s3:{settings.s3_endpoint_url}:{settings.s3_bucket}"
        if store.client is not None
        else f"local:{settings.asset_storage_path.resolve() if settings.asset_storage_path else ''}"
    )
    return hashlib.sha256(location.encode()).hexdigest()


def unversioned(store: ObjectStorage) -> None:
    if store.client is not None:
        result = store.client.get_bucket_versioning(Bucket=store.settings.s3_bucket)
        if result.get("Status") in {"Enabled", "Suspended"}:
            raise DomainError("CLEANUP_VERSIONED_BUCKET", "版本化存储桶须使用单独的保留策略", 409)


def snapshot(store: ObjectStorage, key: str) -> Snapshot | None:
    try:
        if store.client is not None:
            result = store.client.head_object(Bucket=store.settings.s3_bucket, Key=key)
            return Snapshot(
                key,
                result["ContentLength"],
                result["LastModified"].replace(microsecond=0),
                result["ETag"],
            )
        path = store.path(key)
        base = store.settings.asset_storage_path
        assert base is not None
        if any(p.is_symlink() or p.is_junction() for p in [base, path, *path.parents]):
            raise DomainError("CLEANUP_UNSAFE_PATH", "清理路径含符号链接", 409)
        info = path.stat()
        if not path.is_file():
            raise DomainError("CLEANUP_UNSAFE_PATH", "清理目标不是普通文件", 409)
        return Snapshot(
            key,
            info.st_size,
            datetime.fromtimestamp(info.st_mtime, UTC),
            f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}",
        )
    except FileNotFoundError:
        return None
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return None
        raise DomainError("STORAGE_UNAVAILABLE", "清理对象暂不可读", 503) from None
    except (OSError, BotoCoreError):
        raise DomainError("STORAGE_UNAVAILABLE", "清理对象暂不可读", 503) from None


def list_page(
    store: ObjectStorage, prefix: str, cursor: str | None, limit: int
) -> tuple[list[Snapshot], str | None]:
    try:
        unversioned(store)
        if store.client is not None:
            parameters = {"Bucket": store.settings.s3_bucket, "Prefix": prefix, "MaxKeys": limit}
            if cursor:
                parameters["ContinuationToken"] = cursor
            result = store.client.list_objects_v2(**parameters)
            return [
                # S3 HEAD uses HTTP-date (whole seconds); MinIO LIST includes milliseconds.
                Snapshot(
                    row["Key"], row["Size"], row["LastModified"].replace(microsecond=0), row["ETag"]
                )
                for row in result.get("Contents", [])
            ], result.get("NextContinuationToken")
        root = store.path(prefix.rstrip("/"))
        if root.is_symlink() or root.is_junction():
            raise DomainError("CLEANUP_UNSAFE_PATH", "清理目录含符号链接", 409)
        if not root.exists():
            return [], None
        entries = []
        visited = 0
        pending = [root]
        while pending:
            with os.scandir(pending.pop()) as directory:
                for entry in directory:
                    visited += 1
                    if visited > 2000:
                        raise DomainError("CLEANUP_SCAN_LIMIT", "本机目录超过单次安全扫描范围", 409)
                    path = Path(entry.path)
                    if entry.is_symlink() or path.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        base = store.settings.asset_storage_path
                        assert base is not None
                        key = path.relative_to(base).as_posix()
                        if cursor is None or key > cursor:
                            item = snapshot(store, key)
                            if item is not None:
                                entries.append(item)
        page = heapq.nsmallest(limit + 1, entries, key=lambda row: row.key)
        return page[:limit], page[limit - 1].key if len(page) > limit else None
    except (OSError, BotoCoreError, ClientError):
        raise DomainError("STORAGE_UNAVAILABLE", "清理扫描失败，请重试", 503) from None


def delete_snapshot(store: ObjectStorage, expected: Snapshot) -> bool:
    """Caller holds the project gate and has excluded every live upload capability.

    Refuse observed changes. If-Match is additional protection on supporting S3
    servers; MinIO's DELETE does not implement it. Administrators writing directly
    to the store must stop application cleanup before bypassing its write gate.
    """
    try:
        unversioned(store)
        current = snapshot(store, expected.key)
        if current is None:
            return False
        if current != expected:
            raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，请重新扫描", 409)
        if store.client is not None:
            store.client.delete_object(
                Bucket=store.settings.s3_bucket, Key=expected.key, IfMatch=expected.token
            )
        else:
            store.path(expected.key).unlink()
        return True
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] in {409, 412}:
            raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，请重新扫描", 409) from None
        raise DomainError("STORAGE_UNAVAILABLE", "对象清理失败，请重试", 503) from None
    except (OSError, BotoCoreError):
        raise DomainError("STORAGE_UNAVAILABLE", "对象清理失败，请重试", 503) from None
