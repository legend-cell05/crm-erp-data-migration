"""The simulated Atlas Cloud CRM.

A separate service on purpose. A migration demonstrated against a function
call never exercises the parts that actually break: a batch limit, a 429 that
must be obeyed, a partial failure inside an otherwise good batch, an API key,
and a connection that drops in the middle of entity three of four.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from keystone import __version__
from keystone.config import get_settings
from keystone.logging_config import configure_logging, get_logger
from keystone.target.routers import admin, data

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Atlas Cloud CRM (simulated)",
        version=__version__,
        description=(
            "A stand-in for the target CRM. Synthetic data only. Enforces "
            "alternate-key upserts, per-record batch results, a batch size "
            "limit, referential integrity, rate limiting and occasional "
            "outages -- the parts of a real API that a migration must survive."
        ),
    )

    app.include_router(admin.router)
    app.include_router(data.router)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        """A bad request is the client's mistake and must look like one.

        Letting a ValueError escape produces a 500, which tells a migration
        client to retry something that will never succeed.
        """
        return JSONResponse(
            status_code=400,
            content={"code": "BAD_REQUEST", "message": str(exc)},
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": "Atlas Cloud CRM (simulated)",
            "version": __version__,
            "metadata": "/api/v1/$metadata",
            "docs": "/docs",
        }

    logger.info("atlas cloud simulator ready", extra={"version": __version__})
    return app


app = create_app()
