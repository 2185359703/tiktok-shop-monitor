"""FastAPI application factory; import does not connect or initialize schemas."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import router
from app.api.routes_reports import router as reports_router
from app.api.routes_collections import router as collections_router
from app.api.routes_analytics import router as analytics_router
from app.api.routes_images import router as images_router
from app.api.routes_matching import router as matching_router
from app.observability.metrics import Metrics
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory
from app.workers.handlers import handler_registry, validate_enabled


FRONTEND_DIRECTORY = Path(__file__).resolve().parent / "frontend"


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        config = settings or Settings.load()
        validate_enabled(config, handler_registry())
        own_engine = engine is None
        db_engine = engine if engine is not None else make_engine(config.database_url)
        application.state.settings, application.state.engine = config, db_engine
        application.state.factory = make_session_factory(db_engine)
        application.state.metrics = Metrics()
        try:
            yield
        finally:
            if own_engine:
                db_engine.dispose()

    application = FastAPI(title="TikTok Shop 服装监测控制面", version="0.1.0", lifespan=lifespan)
    application.include_router(router)
    application.include_router(reports_router)
    application.include_router(collections_router)
    application.include_router(analytics_router)
    application.include_router(images_router)
    application.include_router(matching_router)
    application.mount("/ui-assets", StaticFiles(directory=FRONTEND_DIRECTORY), name="ui-assets")

    @application.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(FRONTEND_DIRECTORY / "index.html", media_type="text/html")

    @application.exception_handler(RequestValidationError)
    async def bad_input(request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": "invalid_request",
                                                      "fields": [list(item["loc"]) for item in error.errors()]})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        logging.getLogger(__name__).error("database_unavailable", extra={"error_type": type(error).__name__})
        return JSONResponse(status_code=503, content={"error": "database_unavailable"})

    @application.get("/health/live")
    def live():
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready(request: Request):
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "database": "reachable", "collection_enabled": bool(request.app.state.settings.enabled_task_types)}

    @application.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        from app.services.scheduler import task_counts
        metric_set = request.app.state.metrics
        with request.app.state.factory() as db:
            counts = task_counts(db, request.app.state.settings)
        metric_set.queue_depth.clear()
        for kind, status, count in counts:
            metric_set.queue_depth.labels(kind.value, status.value).set(count)
        return Response(generate_latest(metric_set.registry), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
