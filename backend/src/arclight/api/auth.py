import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ConfigDict, Field, field_validator
from sqlalchemy import select, update

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.core.errors import DomainError
from arclight.core.security import (
    DUMMY_HASH,
    decode_token,
    digest,
    mint_token,
    password_hash,
    session_user,
)
from arclight.persistence.models import AuthSession, User


def same_origin(request: Request) -> None:
    # 同源部署；拒绝跨站登录、退出与刷新，不能仅依赖 Cookie 的 SameSite。
    origin = request.headers.get("origin")
    allowed = {str(request.base_url).rstrip("/"), *request.app.state.settings.auth_allowed_origins}
    if request.method != "GET" and origin and origin not in allowed:
        raise DomainError("CROSS_ORIGIN_AUTH", "账号操作必须来自本站", 403)


router = APIRouter(prefix="/api/v1/auth", tags=["auth"], dependencies=[Depends(same_origin)])
COOKIE = "arclight_refresh"


class Credentials(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class Registration(Credentials):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


def public_user(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name}


def issue(db, user, session, request, response) -> dict:
    settings = request.app.state.settings
    refresh = mint_token(user.id, session.id, settings, "refresh")
    session.refresh_hash = digest(refresh)
    response.set_cookie(
        COOKIE,
        refresh,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path="/api/v1/auth",
        max_age=settings.refresh_token_days * 86400,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": mint_token(user.id, session.id, settings, "access"),
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
        "user": public_user(user),
    }


def open_session(db, user, request, response):
    session = AuthSession(
        user_id=user.id,
        refresh_hash="",
        expires_at=datetime.now(UTC)
        + timedelta(days=request.app.state.settings.refresh_token_days),
    )
    db.add(session)
    db.flush()
    return issue(db, user, session, request, response)


@router.post("/register", status_code=201)
def register(payload: Registration, db: Database, request: Request, response: Response) -> dict:
    if not request.app.state.settings.allow_registration:
        raise DomainError("REGISTRATION_DISABLED", "当前环境不开放注册", 403)
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise DomainError("EMAIL_EXISTS", "邮箱已注册，请直接登录", 409)
    user = User(
        email=email,
        display_name=payload.display_name,
        password_hash=password_hash.hash(payload.password),
    )
    db.add(user)
    db.flush()
    return open_session(db, user, request, response)


@router.post("/login")
def login(payload: Credentials, db: Database, request: Request, response: Response) -> dict:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    valid = password_hash.verify(payload.password, user.password_hash if user else DUMMY_HASH)
    if not user or not valid or user.disabled:
        raise DomainError("INVALID_CREDENTIALS", "邮箱或密码不正确", 401)
    return open_session(db, user, request, response)


@router.post("/refresh")
def refresh(db: Database, request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE, "")
    claims = decode_token(token, request.app.state.settings, "refresh")
    # 刷新令牌轮换必须串行，旧令牌不可再次使用。
    session = db.scalar(
        select(AuthSession).where(AuthSession.id == UUID(claims["sid"])).with_for_update()
    )
    user = session_user(db, claims)
    if session is None or not secrets.compare_digest(session.refresh_hash, digest(token)):
        raise DomainError("UNAUTHORIZED", "登录已过期，请重新登录", 401)
    old_hash = digest(token)
    result = issue(db, user, session, request, response)
    new_hash = session.refresh_hash
    # PostgreSQL 行锁之外保留 CAS，让 SQLite 和并发重放也不能刷新两次。
    db.expire(session, ["refresh_hash"])
    changed = db.scalar(
        update(AuthSession)
        .where(
            AuthSession.id == session.id,
            AuthSession.refresh_hash == old_hash,
            AuthSession.revoked.is_(False),
        )
        .values(refresh_hash=new_hash)
        .returning(AuthSession.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("UNAUTHORIZED", "登录已过期，请重新登录", 401)
    return result


@router.post("/logout", status_code=204)
def logout(db: Database, request: Request, response: Response):
    token = request.cookies.get(COOKIE, "")
    kind = "refresh"
    if not token:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            token, kind = value, "access"
    if token:
        try:
            claims = decode_token(token, request.app.state.settings, kind)
        except DomainError:
            claims = None
        if claims:
            session = db.get(AuthSession, UUID(claims["sid"]))
            if session and str(session.user_id) == claims["sub"]:
                session.revoked = True
    response.delete_cookie(COOKIE, path="/api/v1/auth")
    response.headers["Cache-Control"] = "no-store"


@router.get("/me")
def me(db: Database, actor: Actor) -> dict:
    if actor == "service-owner":
        return {"id": actor, "email": "", "display_name": "本地开发者"}
    user = db.get(User, UUID(actor))
    if user is None:
        raise DomainError("UNAUTHORIZED", "请重新登录", 401)
    return public_user(user)
