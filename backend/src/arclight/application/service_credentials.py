"""Credential rotation leaves workflow inputs and production versions immutable."""

from contextlib import nullcontext
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.core.service_keys import Keyring, keyring
from arclight.persistence.models import (
    ModelService,
    ServiceAudit,
    ServiceCredential,
    ServiceKeyState,
    WorkflowRun,
)


def key_state(db: Session) -> ServiceKeyState:
    # Every key write and rotation takes this lock first. Stale processes fail closed.
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE; obtain its writer lock before reading key state.
        db.execute(update(ServiceKeyState).values(active_key_id=ServiceKeyState.active_key_id))
    state = db.scalar(
        select(ServiceKeyState)
        .where(ServiceKeyState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None:
        raise DomainError("KEY_STORAGE_INVALID", "服务凭证迁移尚未就绪，请联系管理员", 409)
    return state


def check_writer(state: ServiceKeyState, ring: Keyring) -> None:
    if state.active_key_id and state.active_key_id != ring.key_id:
        raise DomainError("KEY_STORAGE_STALE", "服务主密钥版本已变化，请管理员更新并重启服务", 409)


def store_credential(db: Session, settings: Settings, secret: str) -> ServiceCredential:
    state = key_state(db)
    ring = keyring(settings)
    check_writer(state, ring)
    state.active_key_id = ring.key_id
    row = ServiceCredential(
        encrypted_key=ring.active.encrypt(secret.encode()).decode(), key_id=ring.key_id
    )
    db.add(row)
    db.flush()
    return row


def read_credential(db: Session | None, settings: Settings, snapshot: dict) -> str:
    ring = keyring(settings)
    try:
        # Runtime holds pending run/event updates while external I/O uses other
        # sessions. Reading a credential must not flush those writes prematurely.
        with db.no_autoflush if db else nullcontext():
            if snapshot.get("credential_id"):
                row = db.get(ServiceCredential, UUID(snapshot["credential_id"])) if db else None
                if row is None:
                    raise ValueError
                return ring.decrypt(row.encrypted_key)
            token = snapshot["encrypted_key"]
            row = (
                db.scalar(
                    select(ServiceCredential).where(
                        ServiceCredential.legacy_digest == sha256(token.encode()).hexdigest()
                    )
                )
                if db
                else None
            )
            return ring.decrypt(row.encrypted_key if row else token)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise DomainError(
            "SERVICE_KEY_UNAVAILABLE", "服务凭证记录缺失或不可读取，请联系管理员", 409
        ) from None


def storage_status(db: Session, settings: Settings) -> dict:
    try:
        ring = keyring(settings)
        state = db.get(ServiceKeyState, 1)
        if state is None:
            raise DomainError("KEY_STORAGE_INVALID", "服务凭证迁移尚未就绪", 409)
        check_writer(state, ring)
    except DomainError as exc:
        return {"code": exc.code, "message": exc.message}
    return {"code": "READY", "message": "服务凭证加密存储可用"}


def credential_snapshot(row: ModelService) -> dict:
    return (
        {"credential_id": str(row.credential_id)}
        if row.credential_id
        else {"encrypted_key": row.encrypted_key}
    )


def legacy_tokens(db: Session):
    # Query only the credential member, never copy story content into operator reports.
    token = WorkflowRun.input_json["_text_service"]["encrypted_key"].as_string()
    for value in db.scalars(select(token).where(token.is_not(None))).yield_per(100):
        if value:
            yield value
    for value in db.scalars(
        select(ModelService.encrypted_key).where(ModelService.encrypted_key.is_not(None))
    ):
        if value:
            yield value


def verify_credentials(db: Session, settings: Settings) -> dict:
    ring = keyring(settings)
    credentials = list(db.scalars(select(ServiceCredential)))
    for row in credentials:
        ring.decrypt(row.encrypted_key)
    tokens = set(legacy_tokens(db))
    for token in tokens:
        read_credential(db, settings, {"encrypted_key": token})
    return {"credentials": len(credentials), "legacy_tokens": len(tokens), "key_id": ring.key_id}


def rotate_credentials(db: Session, settings: Settings, actor: str) -> dict:
    """Caller supplies the transaction; any undecryptable entry rolls everything back."""
    state = key_state(db)
    ring = keyring(settings)
    before = state.active_key_id
    for token in set(legacy_tokens(db)):
        digest = sha256(token.encode()).hexdigest()
        row = db.scalar(select(ServiceCredential).where(ServiceCredential.legacy_digest == digest))
        if row is None:
            row = ServiceCredential(
                encrypted_key=ring.active.encrypt(ring.decrypt(token).encode()).decode(),
                key_id=ring.key_id,
                legacy_digest=digest,
            )
            db.add(row)
    db.flush()
    changed = 0
    for row in db.scalars(select(ServiceCredential).with_for_update()):
        secret = ring.decrypt(row.encrypted_key)
        if row.key_id != ring.key_id:
            row.encrypted_key = ring.active.encrypt(secret.encode()).decode()
            row.key_id = ring.key_id
            row.revision += 1
            changed += 1
    migrated = 0
    for profile in db.scalars(
        select(ModelService).where(ModelService.encrypted_key.is_not(None)).with_for_update()
    ):
        if profile.encrypted_key:
            credential = db.scalar(
                select(ServiceCredential).where(
                    ServiceCredential.legacy_digest
                    == sha256(profile.encrypted_key.encode()).hexdigest()
                )
            )
            assert credential is not None
            profile.credential_id = credential.id
            profile.encrypted_key = None
            profile.revision += 1
            migrated += 1
    state.active_key_id = ring.key_id
    result = {
        "key_id": ring.key_id,
        "previous_key_id": before,
        "rewrapped": changed,
        "migrated_profiles": migrated,
    }
    db.add(ServiceAudit(actor=actor, action="key.rotated", details_json=result))
    db.flush()
    return result
