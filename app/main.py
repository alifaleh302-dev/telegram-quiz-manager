"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, channels, dashboard, publishing, questions, schedules, settings as settings_api
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.services.container import build_services
from app.storage.validators import ValidationError

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    config = get_settings()
    config.ensure_directories()

    services = build_services(config)
    app.state.services = services
    app.state.app_name = config.app_name
    app.state.templates_dir = config.templates_dir
    app.state.static_dir = config.static_dir

    # Reconcile anything left pending by a previous crash before scheduling.
    reconciled = services.publishing.reconcile_pending()
    if reconciled:
        logger.warning("reconciled %s stale pending publish(es)", reconciled)

    services.engine.start()
    await services.bot.start()
    logger.info(
        "%s started (dry_run=%s, data=%s)",
        config.app_name,
        config.dry_run,
        config.data_dir,
    )

    try:
        yield
    finally:
        await services.bot.stop()
        services.engine.shutdown()
        await services.telegram.close()
        logger.info("application stopped")


def create_app() -> FastAPI:
    config = get_settings()
    app = FastAPI(
        title=config.app_name,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.mount("/static", StaticFiles(directory=str(config.static_dir)), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(questions.router)
    app.include_router(channels.router)
    app.include_router(schedules.router)
    app.include_router(publishing.router)
    app.include_router(settings_api.router)

    @app.exception_handler(ValidationError)
    async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
