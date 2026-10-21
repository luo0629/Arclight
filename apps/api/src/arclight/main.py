import logging
import re
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from arclight.api.routes import router
from arclight.core.config import Settings, get_settings
from arclight.core.errors import DomainError
from arclight.persistence.session import build_engine, session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory(engine)
        yield
        engine.dispose()

    app = FastAPI(title="Arclight API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid4().hex
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error(request: Request, code: str, message: str, status: int, details=None):
        response = JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request.state.request_id,
                    "details": details,
                }
            },
        )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return error(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(
            request,
            "VALIDATION_ERROR",
            "请求参数不合法",
            422,
            [{"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()],
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return error(request, "HTTP_ERROR", "请求的资源或方法不可用", exc.status_code)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, _exc: IntegrityError):
        return error(request, "RESOURCE_CONFLICT", "资源重复或引用约束冲突", 409)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error(
            "request_failed request_id=%s error_type=%s",
            request.state.request_id,
            type(exc).__name__,
        )
        return error(request, "INTERNAL_ERROR", "服务暂时无法处理请求", 500)

    @app.get("/health/live", tags=["health"])
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready(request: Request) -> dict:
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            raise DomainError("DATABASE_UNAVAILABLE", "数据库不可用", 503) from None
        return {"status": "ok", "checks": {"database": "ok"}}

    app.include_router(router)
    return app


app = create_app()
