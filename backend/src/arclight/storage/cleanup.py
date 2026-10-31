"""Bounded listings and rechecked deletion for exclusively server-managed keys."""

import base64
import hashlib
import heapq
import json
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


def require_versioning(store: ObjectStorage) -> None:
    if store.client is not None:
        result = store.client.get_bucket_versioning(Bucket=store.settings.s3_bucket)
        if result.get("Status") != "Enabled":
            raise DomainError("CLEANUP_VERSIONING_REQUIRED", "安全清理需要启用存储桶版本化", 409)


def snapshot(store: ObjectStorage, key: str, version_id: str | None = None) -> Snapshot | None:
    try:
        if store.client is not None:
            parameters = {"Bucket": store.settings.s3_bucket, "Key": key}
            if version_id is not None:
                parameters["VersionId"] = version_id
            result = store.client.head_object(**parameters)
            return Snapshot(
                key,
                result["ContentLength"],
                result["LastModified"].replace(microsecond=0),
                "s3:" + result.get("VersionId", "null"),
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
        require_versioning(store)
        if store.client is not None:
            parameters = {"Bucket": store.settings.s3_bucket, "Prefix": prefix, "MaxKeys": limit}
            if cursor:
                try:
                    markers = json.loads(base64.urlsafe_b64decode(cursor))
                    parameters["KeyMarker"] = markers["key"]
                    parameters["VersionIdMarker"] = markers["version"]
                except (ValueError, KeyError, TypeError):
                    raise DomainError(
                        "CLEANUP_CURSOR_CONFLICT", "请重新扫描版本化存储", 409
                    ) from None
            result = store.client.list_object_versions(**parameters)
            rows: dict[str, Snapshot] = {}
            for row in result.get("Versions", []):
                token = "s3:" + row["VersionId"]
                if len(token) > 256:
                    raise DomainError(
                        "CLEANUP_VERSION_UNSUPPORTED", "对象版本标识超过安全清理限制", 409
                    )
                # One exact physical version per key per plan. Later scans can
                # reclaim older versions, including ones hidden by delete markers.
                rows.setdefault(
                    row["Key"],
                    Snapshot(
                        row["Key"], row["Size"], row["LastModified"].replace(microsecond=0), token
                    ),
                )
            next_cursor = None
            if result.get("IsTruncated"):
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "key": result["NextKeyMarker"],
                            "version": result.get("NextVersionIdMarker", ""),
                        }
                    ).encode()
                ).decode()
            return list(rows.values()), next_cursor
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
    """Delete only the captured physical version; never DELETE a mutable S3 key."""
    try:
        require_versioning(store)
        if store.client is None:
            return delete_local_snapshot(store, expected)
        version_id = None
        if store.client is not None:
            if not expected.token.startswith("s3:") or expected.token == "s3:null":
                raise DomainError(
                    "CLEANUP_UNVERSIONED_OBJECT",
                    "旧对象没有不可变版本标识，已保留并需重新规划",
                    409,
                )
            version_id = expected.token[3:]
        current = snapshot(store, expected.key, version_id)
        if current is None:
            return False
        if current != expected:
            raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，请重新扫描", 409)
        if store.client is not None:
            store.client.delete_object(
                Bucket=store.settings.s3_bucket, Key=expected.key, VersionId=version_id
            )
        return True
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] in {409, 412}:
            raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，请重新扫描", 409) from None
        raise DomainError("STORAGE_UNAVAILABLE", "对象清理失败，请重试", 503) from None
    except (OSError, BotoCoreError):
        raise DomainError("STORAGE_UNAVAILABLE", "对象清理失败，请重试", 503) from None


def delete_local_snapshot(store: ObjectStorage, expected: Snapshot) -> bool:
    """Capture by rename, verify the captured inode, recover safely on retry."""
    quarantine_key = (
        "cleanup-quarantine/"
        + hashlib.sha256(f"{expected.key}:{expected.token}".encode()).hexdigest()
    )
    quarantine = store.path(quarantine_key)
    quarantine.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if quarantine.parent.is_symlink() or quarantine.parent.is_junction():
        raise DomainError("CLEANUP_UNSAFE_PATH", "隔离目录无效", 409)
    if not quarantine.exists():
        current = snapshot(store, expected.key)
        if current is None:
            return False
        if current != expected:
            raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，请重新扫描", 409)
        # Only canonical keys are exposed to writers. Replacements at the public
        # key cannot be the inode subsequently removed from private quarantine.
        os.rename(store.path(expected.key), quarantine)
    captured = snapshot(store, quarantine_key)
    matches = captured is not None and (captured.size, captured.modified, captured.token) == (
        expected.size,
        expected.modified,
        expected.token,
    )
    if not matches:
        try:
            os.link(quarantine, store.path(expected.key))
            quarantine.unlink()
        except FileExistsError:
            raise DomainError(
                "CLEANUP_QUARANTINE_PENDING", "并发文件均已保留，需核验后重试恢复", 409
            ) from None
        raise DomainError("CLEANUP_OBJECT_CHANGED", "对象已变化，已恢复并保留", 409)
    quarantine.unlink()
    return True


def recover_local_snapshot(store: ObjectStorage, expected: Snapshot) -> None:
    """Restore a captured file after interruption, before checking new references."""
    if store.client is not None:
        return
    key = (
        "cleanup-quarantine/"
        + hashlib.sha256(f"{expected.key}:{expected.token}".encode()).hexdigest()
    )
    captured = snapshot(store, key)
    if captured is None:
        return
    quarantine = store.path(key)
    try:
        os.link(quarantine, store.path(expected.key))
    except FileExistsError:
        current = snapshot(store, expected.key)
        if current is None or current.token != captured.token:
            raise DomainError(
                "CLEANUP_QUARANTINE_PENDING", "并发文件均已保留，需核验后重试", 409
            ) from None
    except OSError:
        raise DomainError("STORAGE_UNAVAILABLE", "隔离文件恢复失败，请重试", 503) from None
    try:
        quarantine.unlink()
    except OSError:
        raise DomainError("STORAGE_UNAVAILABLE", "隔离文件恢复待完成，请重试", 503) from None
