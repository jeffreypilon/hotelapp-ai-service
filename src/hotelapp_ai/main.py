"""App assembly. Nothing else -- no route logic, no startup side effects beyond wiring. See
architecture-specification.md's folder layout.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.transport.rest import health
from hotelapp_ai.transport.rest.problem import (
    ProblemDetailError,
    problem_detail_exception_handler,
    unhandled_exception_handler,
)


def _configure_logging(log_level: str) -> None:
    level = logging.getLevelName(log_level.upper())
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)

    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    app.state.settings = settings
    app.state.http_client = client
    app.state.gateway = HotelAppGateway(client, settings.hotelapp_api_base_url)

    try:
        yield
    finally:
        await client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="HotelApp AI Service", lifespan=lifespan)

    # Never a wildcard -- only added once real origins are configured.
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_exception_handler(ProblemDetailError, problem_detail_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(health.router, prefix="/api/v1")

    return app


app = create_app()
