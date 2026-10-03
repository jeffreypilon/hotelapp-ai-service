"""`POST /api/v1/assistant/ask` -- SSE framing, the three event types, and the pre-stream /
mid-stream error split from error-handling.md §3.

The only module permitted to format an SSE line for this route -- see module-registry.md's
`rest/assistant.py` entry. No model call and no retrieval happens here; every event this module
emits was produced by `services/assistant.AssistantService`.

> This module deliberately imports only from `domain/` alongside FastAPI/pydantic, and never
> imports `AssistantService` itself for typing -- `request.app.state.assistant_service` is used
> untyped, the same way `transport/rest/health.py` reaches `request.app.state.gateway`. Importing
> `hotelapp_ai.services.assistant` would also execute its own imports (`httpx` via
> `services/llm_client.py`, `psycopg` via `repositories/database.py`), which is exactly what
> module-registry.md's import rule forbids in `transport/` -- and import-linter's forbidden-module
> check is transitive, so it would catch this even though the forbidden names never appear in this
> file's own `import` statements.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Literal

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hotelapp_ai.domain.assistant_events import (
    AssistantEvent,
    CitationEvent,
    ConversationTurn,
    DoneEvent,
    TokenEvent,
)
from hotelapp_ai.domain.errors import (
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QuestionTooLongError,
    RetrievalFailedError,
)
from hotelapp_ai.transport.rest.problem import ProblemDetailError

logger = structlog.get_logger(__name__)

router = APIRouter()


class ConversationTurnModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class AskRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    question: str
    history: list[ConversationTurnModel] | None = None


def _format_sse(event_name: str, payload: dict[str, object]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()


def _format_event(event: AssistantEvent) -> bytes:
    if isinstance(event, TokenEvent):
        return _format_sse("token", {"text": event.text})
    if isinstance(event, CitationEvent):
        return _format_sse(
            "citation",
            {
                "documentTitle": event.document_title,
                "section": event.section,
                "chunkId": event.chunk_id,
            },
        )
    if isinstance(event, DoneEvent):
        return _format_sse(
            "done",
            {
                "usage": {
                    "promptTokens": event.prompt_tokens,
                    "completionTokens": event.completion_tokens,
                },
                "costUsd": event.cost_usd,
                "cacheHit": event.cache_hit,
            },
        )
    return _format_sse(
        "error",
        {"code": event.code, "detail": event.detail, "traceId": str(uuid.uuid4())},
    )


_PRE_STREAM_STATUS_AND_CODE: dict[type[Exception], tuple[int, str]] = {
    QuestionTooLongError: (400, "QUESTION_TOO_LONG"),
    RetrievalFailedError: (503, "RETRIEVAL_FAILED"),
    ProviderTimeoutError: (504, "AI_TIMEOUT"),
    ProviderRateLimitedError: (429, "AI_RATE_LIMITED"),
    ProviderContentFilteredError: (422, "AI_CONTENT_FILTERED"),
    ProviderUnavailableError: (503, "AI_UNAVAILABLE"),
}


@router.post("/assistant/ask")
async def ask(request: Request, body: AskRequest) -> StreamingResponse:
    # Untyped on purpose -- see the module docstring's note on why this module never imports
    # `AssistantService`.
    assistant_service = request.app.state.assistant_service
    history = [
        ConversationTurn(role=turn.role, content=turn.content) for turn in (body.history or [])
    ]

    event_generator = assistant_service.ask(question=body.question, history=history)

    # Peeking the first event is what makes the pre-stream / mid-stream split real rather than
    # documented-only: any exception raised before `AssistantService.ask`'s first `yield` --
    # validation, retrieval, grading, the bounded retry -- surfaces here, before any byte of the
    # response has been sent, and becomes an ordinary Problem Details response. Anything after
    # this point was already caught and turned into an `ErrorEvent` by `ask` itself, per
    # error-handling.md §3.
    try:
        first_event: AssistantEvent | None = await anext(event_generator)
    except StopAsyncIteration:
        first_event = None
    except Exception as exc:  # noqa: BLE001 -- re-raised as a specific ProblemDetailError below.
        status, code = _PRE_STREAM_STATUS_AND_CODE.get(type(exc), (500, "INTERNAL_ERROR"))
        logger.warning("assistant.ask.pre_stream_failure", code=code, error=str(exc))
        raise ProblemDetailError(status=status, code=code, detail=str(exc)) from exc

    async def event_stream() -> AsyncIterator[bytes]:
        if first_event is not None:
            yield _format_event(first_event)
        async for event in event_generator:
            yield _format_event(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
