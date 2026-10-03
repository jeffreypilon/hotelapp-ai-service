"""App assembly. Nothing else -- no route logic, only startup wiring plus schema validation."""

from __future__ import annotations

import functools
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.repositories.database import assert_ai_schema_ready
from hotelapp_ai.services.assistant import AssistantDependencies, AssistantService
from hotelapp_ai.services.ingestion import OpenAIEmbeddingProvider
from hotelapp_ai.services.llm_client import LLMClient
from hotelapp_ai.services.reranking import LazyReranker
from hotelapp_ai.services.search import SearchDependencies
from hotelapp_ai.services.search import search as run_search
from hotelapp_ai.transport.rest import assistant, health, search
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
    assert_ai_schema_ready(settings.require_database_url())

    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    app.state.settings = settings
    app.state.http_client = client
    app.state.gateway = HotelAppGateway(client, settings.hotelapp_api_base_url)
    app.state.retrieval_status = "UP"

    # `OPENAI_API_KEY` absent is a valid, designed state -- see settings.py and
    # error-handling.md §4. `embedding_provider`/`llm_client` stay `None` in that case;
    # `AssistantService.ask` reports `AI_UNAVAILABLE` before touching either, and the reranker
    # stays unloaded (`LazyReranker`) since retrieval is never reached.
    embedding_provider: OpenAIEmbeddingProvider | None = None
    llm_client: LLMClient | None = None
    if settings.provider_configured:
        assert settings.openai_api_key is not None
        embedding_provider = OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key, model=settings.embedding_model
        )
        llm_client = LLMClient(api_key=settings.openai_api_key)

    assistant_deps = AssistantDependencies(
        database_url=settings.require_database_url(),
        embedding_provider=embedding_provider,
        reranker=LazyReranker(),
        llm_client=llm_client,
        generation_model=settings.generation_model,
        grading_model=settings.grading_model,
    )
    app.state.assistant_service = AssistantService(
        assistant_deps, question_max_length=settings.question_max_length
    )

    search_deps = SearchDependencies(
        gateway=app.state.gateway,
        llm_client=llm_client,
        # F2 parameter extraction shares the nano tier with rewrite/grading --
        # ai-enablement-overview.md §11 groups all three under one row, not three settings.
        extraction_model=settings.grading_model,
    )
    app.state.search_service = functools.partial(run_search, search_deps)

    try:
        yield
    finally:
        await client.aclose()
        if embedding_provider is not None:
            embedding_provider.close()
        if llm_client is not None:
            await llm_client.aclose()


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
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")

    return app


app = create_app()
