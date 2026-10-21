import time
from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestProtection:
    """单 API 进程的请求体与账号限流；多副本部署还须在统一网关限流。"""

    def __init__(self, app: ASGIApp, max_bytes: int, auth_per_minute: int):
        self.app = app
        self.max_bytes = max_bytes
        self.auth_per_minute = auth_per_minute
        self.attempts: dict[str, deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def reject(status, code, message, headers=None):
            response = JSONResponse(
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "request_id": scope.get("state", {}).get("request_id", ""),
                        "details": None,
                    }
                },
                status_code=status,
                headers=headers,
            )
            await response(scope, receive, send)

        if scope["method"] == "POST" and scope["path"] in {
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/auth/refresh",
        }:
            now = time.monotonic()
            client = scope.get("client")
            key = client[0] if client else "unknown"
            # 清理冷却完毕的地址，避免长期保留客户端标识。
            self.attempts = {
                ip: hits for ip, hits in self.attempts.items() if hits and hits[-1] > now - 60
            }
            if key not in self.attempts and len(self.attempts) >= 10000:
                return await reject(
                    429, "RATE_LIMITED", "请求繁忙，请稍后重试", {"Retry-After": "60"}
                )
            hits = self.attempts.setdefault(key, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.auth_per_minute:
                return await reject(
                    429, "RATE_LIMITED", "账号请求过于频繁，请稍后重试", {"Retry-After": "60"}
                )
            hits.append(now)

        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if b"content-length" in headers:
            try:
                length = int(headers[b"content-length"])
            except ValueError:
                return await reject(400, "INVALID_CONTENT_LENGTH", "请求长度无效")
            if length < 0:
                return await reject(400, "INVALID_CONTENT_LENGTH", "请求长度无效")
            if length > self.max_bytes:
                return await reject(413, "REQUEST_TOO_LARGE", "请求体超过服务限制")
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                return await reject(413, "REQUEST_TOO_LARGE", "请求体超过服务限制")
            chunks.append(chunk)
            if not event.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
