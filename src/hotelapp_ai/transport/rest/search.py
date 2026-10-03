"""`POST /api/v1/assistant/search` -- natural-language availability search request/response
shapes, and the Problem Details mapping for everything `services/search.py` can raise.

The only module permitted to shape this route's request/response bodies -- see
module-registry.md's `rest/search.py` entry. No model call and no backend call happens here;
every field in the response was produced by `services/search.SearchResult`.

> Like `rest/assistant.py`, this module reaches `request.app.state.search_service` untyped and
> never imports `hotelapp_ai.services.search` for typing -- importing it would transitively pull
> in `httpx` (via `gateways/hotelapp.py`) and `openai`-shaped code (via `services/llm_client.py`),
> which module-registry.md's import rule forbids in `transport/`.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hotelapp_ai.domain.errors import (
    BackendProblemError,
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SearchParamsIncompleteError,
)
from hotelapp_ai.transport.rest.problem import ProblemDetailError

logger = structlog.get_logger(__name__)

router = APIRouter()


class SearchRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    query: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    interpretation: str
    parameters: dict[str, Any]
    results: dict[str, Any]


_STATUS_AND_CODE: dict[type[Exception], tuple[int, str]] = {
    SearchParamsIncompleteError: (400, "VALIDATION_FAILED"),
    ProviderTimeoutError: (504, "AI_TIMEOUT"),
    ProviderRateLimitedError: (429, "AI_RATE_LIMITED"),
    ProviderContentFilteredError: (422, "AI_CONTENT_FILTERED"),
    ProviderUnavailableError: (503, "AI_UNAVAILABLE"),
}


@router.post("/assistant/search", response_model=SearchResponse)
async def post_search(request: Request, body: SearchRequest) -> SearchResponse:
    # Untyped on purpose -- see the module docstring's note on why this module never imports
    # `services/search.py`.
    search_service = request.app.state.search_service
    cookie_header = request.headers.get("cookie")

    try:
        result = await search_service(query=body.query, cookie_header=cookie_header)
    except BackendProblemError as exc:
        # Translated, never re-interpreted -- error-handling.md §2. The same status/code/detail
        # `GET /availability` itself produced, round-tripped through this endpoint unchanged.
        logger.warning("assistant.search.backend_problem", code=exc.code, status=exc.status)
        raise ProblemDetailError(status=exc.status, code=exc.code, detail=exc.detail) from exc
    except Exception as exc:  # noqa: BLE001 -- re-raised as a specific ProblemDetailError below.
        status, code = _STATUS_AND_CODE.get(type(exc), (500, "INTERNAL_ERROR"))
        logger.warning("assistant.search.failure", code=code, error=str(exc))
        raise ProblemDetailError(status=status, code=code, detail=str(exc)) from exc

    return SearchResponse(
        interpretation=result.interpretation,
        parameters=result.parameters,
        results=result.results,
    )
