"""Unit tests for GET /api/v1/assistant/health. No infrastructure needed -- the backend call is
stubbed by monkeypatching the gateway, not by mocking retrieval or a database (neither exists yet).
"""

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
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()


def test_health_reports_backend_up(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "retrieval": "NOT_CONFIGURED",
        "backend": "UP",
        "backendTarget": "springboot",
    }


def test_health_reports_backend_down(monkeypatch: pytest.MonkeyPatch) -> None:
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
