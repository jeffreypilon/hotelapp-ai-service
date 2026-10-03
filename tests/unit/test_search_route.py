"""Unit tests for `POST /api/v1/assistant/search`'s request/response shaping and Problem Details
mapping -- `app.state.search_service` is swapped for a fake coroutine, so these tests exercise
routing and status-code mapping only, never a real extraction call or backend.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.domain.errors import (
    BackendProblemError,
    ProviderUnavailableError,
    SearchParamsIncompleteError,
)
from hotelapp_ai.main import create_app
from hotelapp_ai.services.search import SearchResult


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOTELAPP_API_BASE_URL", "http://localhost:8080/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "******localhost:5433/hotelapp")
    get_settings.cache_clear()
    monkeypatch.setattr("hotelapp_ai.main.assert_ai_schema_ready", lambda database_url: None)


def test_search_returns_interpretation_parameters_and_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SearchResult(
        interpretation="2 guests \u00b7 2026-11-14 \u2013 2026-11-16",
        parameters={"propertyId": "p1", "checkInDate": "2026-11-14", "numGuests": 2},
        results={"data": [], "pagination": {}},
    )

    async def fake_search_service(*, query: str, cookie_header: str | None) -> SearchResult:
        return result

    app = create_app()
    with TestClient(app) as client:
        client.app.state.search_service = fake_search_service
        response = client.post("/api/v1/assistant/search", json={"query": "a room for two"})

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"] == result.interpretation
    assert body["parameters"]["numGuests"] == 2
    assert body["results"] == {"data": [], "pagination": {}}


def test_search_missing_dates_is_400_validation_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    error = SearchParamsIncompleteError("I didn't catch your dates.")

    async def fake_search_service(*, query: str, cookie_header: str | None) -> SearchResult:
        raise error

    app = create_app()
    with TestClient(app) as client:
        client.app.state.search_service = fake_search_service
        response = client.post("/api/v1/assistant/search", json={"query": "a quiet room"})

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert response.json()["detail"] == "I didn't catch your dates."


def test_search_backend_problem_passes_through_unmodified(monkeypatch: pytest.MonkeyPatch) -> None:
    error = BackendProblemError(status=404, code="NOT_FOUND", detail="No such property.")

    async def fake_search_service(*, query: str, cookie_header: str | None) -> SearchResult:
        raise error

    app = create_app()
    with TestClient(app) as client:
        client.app.state.search_service = fake_search_service
        response = client.post("/api/v1/assistant/search", json={"query": "a room"})

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert response.json()["detail"] == "No such property."


def test_search_provider_unavailable_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ProviderUnavailableError("No OPENAI_API_KEY is configured.")

    async def fake_search_service(*, query: str, cookie_header: str | None) -> SearchResult:
        raise error

    app = create_app()
    with TestClient(app) as client:
        client.app.state.search_service = fake_search_service
        response = client.post("/api/v1/assistant/search", json={"query": "a room"})

    assert response.status_code == 503
    assert response.json()["code"] == "AI_UNAVAILABLE"
