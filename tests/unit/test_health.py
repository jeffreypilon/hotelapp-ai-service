"""Unit tests for GET /api/v1/assistant/health."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.main import create_app


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the settings cache AND pin provider configuration explicitly.

    Without the setenv, these tests inherit whatever the developer has in their environment or
    .env -- so they pass on a machine with no OPENAI_API_KEY and fail on one that has a real key.
    That failure mode is the wrong way round: green in CI, red locally, which is how a suite stops
    being believed. An environment variable takes precedence over .env in pydantic-settings, so
    setting it empty here isolates the test from both.
    """
    monkeypatch.setenv("HOTELAPP_API_BASE_URL", "http://localhost:8080/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/hotelapp")
    get_settings.cache_clear()


def test_health_reports_backend_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hotelapp_ai.main.assert_ai_schema_ready", lambda database_url: None)

    async def fake_get_properties(
        self: HotelAppGateway, *, cookie_header: str | None = None
    ) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr(HotelAppGateway, "get_properties", fake_get_properties)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/assistant/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "UP",
        "provider": "NOT_CONFIGURED",
        "retrieval": "UP",
        "backend": "UP",
        "backendTarget": "springboot",
    }


def test_health_reports_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hotelapp_ai.main.assert_ai_schema_ready", lambda database_url: None)

    async def fake_get_properties(
        self: HotelAppGateway, *, cookie_header: str | None = None
    ) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(HotelAppGateway, "get_properties", fake_get_properties)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/assistant/health")

    assert response.status_code == 200
    assert response.json()["backend"] == "DOWN"


def test_health_reports_provider_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hotelapp_ai.main.assert_ai_schema_ready", lambda database_url: None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()

    async def fake_get_properties(
        self: HotelAppGateway, *, cookie_header: str | None = None
    ) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr(HotelAppGateway, "get_properties", fake_get_properties)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/assistant/health")

    assert response.json()["provider"] == "UP"


def test_startup_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        with TestClient(create_app()):
            pass
