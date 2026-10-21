import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.core.security import decode_token, session_user

security = HTTPBearer(auto_error=False)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory.begin() as session:
        yield session


Database = Annotated[Session, Depends(get_session, scope="function")]


def current_actor(
    request: Request,
    db: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> str:
    settings = request.app.state.settings
    if credentials is None:
        raise DomainError("UNAUTHORIZED", "请先登录", 401)
    token = credentials.credentials
    # 开发服务令牌仅用于本地工具；生产身份必须来自可撤销的用户会话。
    if settings.environment != "production" and secrets.compare_digest(
        token, settings.api_token.get_secret_value()
    ):
        return "service-owner"
    return str(session_user(db, decode_token(token, settings, "access")).id)


Actor = Annotated[str, Depends(current_actor)]
