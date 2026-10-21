import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.persistence.models import AuthSession, User

password_hash = PasswordHash.recommended()
DUMMY_HASH = password_hash.hash("arclight-dummy-password-never-used")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def decode_token(token: str, settings: Settings, kind: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience="arclight",
            issuer="arclight-api",
            options={"require": ["exp", "iat", "sub", "sid", "kind", "jti"]},
        )
        if claims["kind"] != kind:
            raise ValueError("wrong token kind")
        UUID(claims["sub"])
        UUID(claims["sid"])
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise DomainError("UNAUTHORIZED", "登录已过期，请重新登录", 401) from None


def session_user(db: Session, claims: dict) -> User:
    session = db.get(AuthSession, UUID(claims["sid"]))
    user = db.get(User, UUID(claims["sub"]))
    if (
        session is None
        or session.revoked
        or user is None
        or user.disabled
        or session.user_id != user.id
        or session.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    ):
        raise DomainError("UNAUTHORIZED", "登录已过期，请重新登录", 401)
    return user


def mint_token(user_id: UUID, session_id: UUID, settings: Settings, kind: str) -> str:
    now = datetime.now(UTC)
    duration = (
        timedelta(minutes=settings.access_token_minutes)
        if kind == "access"
        else timedelta(days=settings.refresh_token_days)
    )
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "kind": kind,
            "jti": uuid4().hex,
            "iat": now,
            "exp": now + duration,
            "iss": "arclight-api",
            "aud": "arclight",
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
