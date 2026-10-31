"""Revocable, exact-file tickets. No storage credentials reach the browser."""

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.core.security import decode_token, digest, session_user
from arclight.persistence.models import Project, ProjectMember, StoredFile, UploadIntent
from arclight.storage.transfer_protocol import TransferGrant
from arclight.storage.transfers import MAX_TRANSFER_SECONDS, presign_download, signing_store


def membership_stamp(db: Session, project: Project, actor: str) -> str:
    if project.owner_id == actor:
        membership = "owner:" + actor
    else:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.user_id == UUID(actor)
            )
        )
        if member is None:
            raise DomainError("TRANSFER_REVOKED", "文件访问授权已失效", 403)
        changed_at = member.updated_at.replace(tzinfo=UTC).isoformat()
        membership = f"{member.id}:{member.role}:{changed_at}"
    return digest(f"{project.id}:{project.deletion_id}:{membership}")


def file_stamp(row: StoredFile | UploadIntent) -> str:
    media_type = row.declared_media_type if isinstance(row, UploadIntent) else row.media_type
    return digest(
        f"{row.id}:{row.project_id}:{row.bucket}:{row.object_key}:"
        f"{row.byte_size}:{row.checksum}:{media_type}:{row.original_name}"
    )


def issue_ticket(
    db: Session,
    settings: Settings,
    project: Project,
    row: StoredFile | UploadIntent,
    actor: str,
    bearer: str,
    seconds: int,
) -> tuple[str, datetime]:
    # Validate real private storage configuration at issuance, including historical S3 files.
    signing_store(settings, row.bucket or settings.s3_bucket)
    now = int(datetime.now(UTC).timestamp())
    expires = now + min(seconds, MAX_TRANSFER_SECONDS)
    if actor == "service-owner":
        if settings.environment == "production" or not secrets.compare_digest(
            bearer, settings.api_token.get_secret_value()
        ):
            raise DomainError("UNAUTHORIZED", "文件访问授权已失效", 401)
        session_id = digest(bearer)
    else:
        access = decode_token(bearer, settings, "access")
        session_id = access["sid"]
        expires = min(expires, int(access["exp"]))
    token = jwt.encode(
        {
            "iss": "arclight-api",
            "aud": "arclight-file-transfer",
            "sub": actor,
            "sid": session_id,
            "iat": now,
            "exp": expires,
            "jti": uuid4().hex,
            "pid": str(project.id),
            "rid": str(row.id),
            "method": "POST" if isinstance(row, UploadIntent) else "GET",
            "membership": membership_stamp(db, project, actor),
            "file": file_stamp(row),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return settings.file_transfer_base_url + "/" + token, datetime.fromtimestamp(expires, UTC)


def authorize_transfer(db: Session, settings: Settings, token: str, method: str) -> TransferGrant:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience="arclight-file-transfer",
            issuer="arclight-api",
            options={
                "require": [
                    "sub",
                    "sid",
                    "iat",
                    "exp",
                    "jti",
                    "pid",
                    "rid",
                    "method",
                    "membership",
                    "file",
                ]
            },
        )
        if claims["method"] != method or method not in {"POST", "GET"}:
            raise ValueError("wrong action")
        if not 0 < claims["exp"] - claims["iat"] <= MAX_TRANSFER_SECONDS:
            raise ValueError("invalid lifetime")
        if not all(isinstance(claims[key], str) for key in ("membership", "file", "sid")):
            raise ValueError("invalid binding")
        project_id, resource_id = UUID(claims["pid"]), UUID(claims["rid"])
        actor = claims["sub"]
        if actor == "service-owner":
            if settings.environment == "production" or not secrets.compare_digest(
                claims["sid"], digest(settings.api_token.get_secret_value())
            ):
                raise ValueError("revoked service token")
        else:
            UUID(actor)
            UUID(claims["sid"])
            session_user(db, claims)
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise DomainError("TRANSFER_REVOKED", "文件访问授权已失效", 403) from None
    project = require_project(db, project_id, actor, write=method == "POST")
    if not secrets.compare_digest(claims["membership"], membership_stamp(db, project, actor)):
        raise DomainError("TRANSFER_REVOKED", "文件访问授权已失效", 403)
    row: UploadIntent | StoredFile | None = (
        db.get(UploadIntent, resource_id) if method == "POST" else db.get(StoredFile, resource_id)
    )
    if row is None or row.project_id != project_id or file_stamp(row) != claims["file"]:
        raise DomainError("TRANSFER_REVOKED", "文件访问授权已失效", 403)
    seconds = max(1, int(claims["exp"] - datetime.now(UTC).timestamp()))
    if isinstance(row, UploadIntent):
        if row.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
            raise DomainError("TRANSFER_REVOKED", "文件访问授权已失效", 403)
        store = signing_store(settings, row.bucket)
        assert store.client is not None
        headers = {"Content-Type": row.declared_media_type, "x-amz-meta-sha256": row.checksum}
        url = store.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": row.bucket,
                "Key": row.object_key,
                "ContentType": row.declared_media_type,
                "ContentLength": row.byte_size,
                "Metadata": {"sha256": row.checksum},
            },
            ExpiresIn=seconds,
            HttpMethod="PUT",
        )
        return TransferGrant(method="PUT", url=url, headers=headers, byte_size=row.byte_size)
    return TransferGrant(
        method="GET",
        url=presign_download(
            settings, row.bucket or "", row.object_key, row.original_name, seconds
        ),
        byte_size=row.byte_size,
    )
