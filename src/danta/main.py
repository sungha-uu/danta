from __future__ import annotations

from fastapi import FastAPI

from danta import __version__
from danta.api.routes import router
from danta.config import load_settings
from danta.logging import configure_logging


def create_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Danta",
        version=__version__,
        description="KIS paper-trading research and controlled execution service",
    )
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()

