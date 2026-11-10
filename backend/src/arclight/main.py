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

from arclight.api.art_direction import router as art_direction_router
from arclight.api.art_proposals import router as art_proposals_router
from arclight.api.asset_usage import router as asset_usage_router
from arclight.api.asset_versions import router as asset_versions_router
from arclight.api.assets import router as assets_router
from arclight.api.auth import router as auth_router
from arclight.api.canvas import router as canvas_router
from arclight.api.character_matching import router as character_matching_router
from arclight.api.collaboration import router as collaboration_router
from arclight.api.content import router as content_router
from arclight.api.contexts import router as contexts_router
from arclight.api.continuity_comparison import router as continuity_comparison_router
from arclight.api.continuity_revisions import router as continuity_revisions_router
from arclight.api.continuity_workflow import router as continuity_workflow_router
from arclight.api.costs import router as costs_router
from arclight.api.dependencies_graph import router as dependencies_router
from arclight.api.director import router as director_router
from arclight.api.file_transfers import router as file_transfers_router
from arclight.api.files import router as files_router
from arclight.api.generation import router as generation_router
from arclight.api.generation_budget import router as generation_budget_router
from arclight.api.generation_candidates import router as generation_candidates_router
from arclight.api.human_reviews import router as human_reviews_router
from arclight.api.model_gateway import router as model_gateway_router
from arclight.api.model_services import router as model_services_router
from arclight.api.production_state import router as production_state_router
from arclight.api.production_versions import router as production_versions_router
from arclight.api.project_deletion import router as project_deletion_router
from arclight.api.project_overview import router as project_overview_router
from arclight.api.protection import RequestProtection
from arclight.api.render_manifests import router as render_manifests_router
from arclight.api.render_previews import router as render_previews_router
from arclight.api.rights import router as rights_router
from arclight.api.routes import router
from arclight.api.screenwriter import router as screenwriter_router
from arclight.api.script_rewrite import router as script_rewrite_router
from arclight.api.storage_cleanup import router as storage_cleanup_router
from arclight.api.story_analysis import router as story_analysis_router
from arclight.api.story_bible import router as story_bible_router
from arclight.api.story_outline import router as story_outline_router
from arclight.api.story_sources import router as story_sources_router
from arclight.api.timelines import router as timelines_router
from arclight.api.transfer_authorization import router as transfer_authorization_router
from arclight.api.upload_completion import router as upload_completion_router
from arclight.api.visual_bible import router as visual_bible_router
from arclight.core.config import Settings, get_settings
from arclight.core.errors import DomainError
from arclight.persistence.session import build_engine, session_factory
from arclight.storage.objects import ObjectStorage

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory(engine, settings)
        yield
        engine.dispose()

    app = FastAPI(title="Arclight API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        RequestProtection,
        max_bytes=settings.max_request_bytes,
        auth_per_minute=settings.auth_requests_per_minute,
    )

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
        checks = {"database": "ok"}
        if settings.object_storage_backend == "s3":
            ObjectStorage(settings).check()
            checks["object_storage"] = "ok"
        return {"status": "ok", "checks": checks}

    app.include_router(model_services_router)
    app.include_router(model_gateway_router)
    app.include_router(auth_router)
    app.include_router(assets_router)
    app.include_router(art_proposals_router)
    app.include_router(art_direction_router)
    app.include_router(files_router)
    app.include_router(generation_router)
    app.include_router(generation_candidates_router)
    app.include_router(continuity_revisions_router)
    app.include_router(continuity_workflow_router)
    app.include_router(continuity_comparison_router)
    app.include_router(human_reviews_router)
    app.include_router(generation_budget_router)
    app.include_router(costs_router)
    app.include_router(production_versions_router)
    app.include_router(rights_router)
    app.include_router(storage_cleanup_router)
    app.include_router(render_previews_router)
    app.include_router(timelines_router)
    app.include_router(render_manifests_router)
    app.include_router(file_transfers_router)
    app.include_router(transfer_authorization_router)
    app.include_router(upload_completion_router)
    app.include_router(dependencies_router)
    app.include_router(contexts_router)
    app.include_router(asset_usage_router)
    app.include_router(visual_bible_router)
    app.include_router(asset_versions_router)
    app.include_router(router)
    app.include_router(collaboration_router)
    app.include_router(content_router)
    app.include_router(canvas_router)
    app.include_router(story_bible_router)
    app.include_router(project_deletion_router)
    app.include_router(story_sources_router)
    app.include_router(story_analysis_router)
    app.include_router(story_outline_router)
    app.include_router(script_rewrite_router)
    app.include_router(screenwriter_router)
    app.include_router(director_router)
    app.include_router(character_matching_router)
    app.include_router(project_overview_router)
    app.include_router(production_state_router)
    return app


app = create_app()
