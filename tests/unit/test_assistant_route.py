"""Unit tests for `POST /api/v1/assistant/ask`'s SSE framing and the pre-stream / mid-stream
error split -- error-handling.md §3. `app.state.assistant_service` is swapped for a fake that
yields a scripted event sequence, so these tests exercise framing and status-code mapping only,
never a real graph or provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.domain.assistant_events import CitationEvent, DoneEvent, ErrorEvent, TokenEvent
from hotelapp_ai.domain.errors import ProviderUnavailableError, QuestionTooLongError
from hotelapp_ai.main import create_app


class _ScriptedAssistantService:
    def __init__(self, events_or_error: list[object] | Exception) -> None:
        self._events_or_error = events_or_error

    async def ask(self, *, question: str, history: list[object]) -> AsyncIterator[Any]:
        if isinstance(self._events_or_error, Exception):
            raise self._events_or_error
        for event in self._events_or_error:
            yield event


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOTELAPP_API_BASE_URL", "http://localhost:8080/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "******localhost:5433/hotelapp")
    get_settings.cache_clear()
    monkeypatch.setattr("hotelapp_ai.main.assert_ai_schema_ready", lambda database_url: None)


def _client_with_scripted_service(
    monkeypatch: pytest.MonkeyPatch, events_or_error: list[object] | Exception
) -> TestClient:
    monkeypatch.setattr(
        "hotelapp_ai.main.AssistantService",
        lambda deps, *, question_max_length: _ScriptedAssistantService(events_or_error),
    )
    return TestClient(create_app())


def test_ask_streams_token_citation_and_done_events(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = [
        CitationEvent(number=1, document_title="House Rules", section="Pets", chunk_id="chunk-1"),
        TokenEvent(text="Yes, dogs are welcome."),
        DoneEvent(prompt_tokens=10, completion_tokens=5, cost_usd="0.001"),
    ]
    with _client_with_scripted_service(monkeypatch, events) as client:
        response = client.post("/api/v1/assistant/ask", json={"question": "Can I bring my dog?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: citation" in body
    assert '"documentTitle": "House Rules"' in body
    assert "event: token" in body
    assert '"text": "Yes, dogs are welcome."' in body
    assert "event: done" in body
    assert '"costUsd": "0.001"' in body
    # The event ordering in the response mirrors the generator's own order exactly.
    assert body.index("event: citation") < body.index("event: token") < body.index("event: done")


def test_ask_mid_stream_failure_emits_terminal_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = [
        CitationEvent(number=1, document_title="House Rules", section="Pets", chunk_id="chunk-1"),
        ErrorEvent(code="AI_TIMEOUT", detail="provider took too long"),
    ]
    with _client_with_scripted_service(monkeypatch, events) as client:
        response = client.post("/api/v1/assistant/ask", json={"question": "Can I bring my dog?"})

    # A mid-stream failure must still be a 200 with a terminal `error` event -- the status code
    # was already sent by the time the failure happened, per error-handling.md §3.
    assert response.status_code == 200
    assert "event: citation" in response.text
    assert "event: error" in response.text
    assert '"code": "AI_TIMEOUT"' in response.text
    assert "traceId" in response.text


def test_ask_pre_stream_question_too_long_is_an_ordinary_problem_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = QuestionTooLongError("Question exceeds 5 characters.")
    with _client_with_scripted_service(monkeypatch, error) as client:
        response = client.post("/api/v1/assistant/ask", json={"question": "way too long"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "QUESTION_TOO_LONG"


def test_ask_pre_stream_provider_unavailable_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ProviderUnavailableError("No OPENAI_API_KEY is configured.")
    with _client_with_scripted_service(monkeypatch, error) as client:
        response = client.post("/api/v1/assistant/ask", json={"question": "Can I bring my dog?"})

    assert response.status_code == 503
    assert response.json()["code"] == "AI_UNAVAILABLE"
