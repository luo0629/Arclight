import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError

security = HTTPBearer(auto_error=False)


def current_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> str:
    expected = request.app.state.settings.api_token.get_secret_value()
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise DomainError("UNAUTHORIZED", "需要有效的 Bearer Token", 401)
    # 当前是单租户服务令牌。多用户 JWT/RBAC 必须替换此依赖，不能信任客户端用户 ID。
    return "service-owner"


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory.begin() as session:
        yield session


Actor = Annotated[str, Depends(current_actor)]
Database = Annotated[Session, Depends(get_session, scope="function")]
