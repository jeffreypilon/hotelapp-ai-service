"""Unit tests for the two gateway methods AI Step 5 added: `get_property` and `list_room_types`.
`get_properties`/`list_properties`/`get_availability`/`check_backend_reachable` already existed
and are exercised indirectly by `tests/unit/test_search.py` and `tests/unit/test_health.py`.
"""

from __future__ import annotations

import httpx
import pytest

from hotelapp_ai.domain.errors import BackendProblemError
from hotelapp_ai.gateways.hotelapp import HotelAppGateway


def _gateway(handler: httpx.MockTransport) -> HotelAppGateway:
    client = httpx.AsyncClient(transport=handler, base_url="http://backend")
    return HotelAppGateway(client, "http://backend/api/v1")


async def test_get_property_returns_the_parsed_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/properties/p1"
        return httpx.Response(200, json={"id": "p1", "name": "Harborview Grand"})

    gateway = _gateway(httpx.MockTransport(handle))

    result = await gateway.get_property("p1")

    assert result == {"id": "p1", "name": "Harborview Grand"}


async def test_get_property_forwards_the_cookie() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie") == "sessionId=abc"
        return httpx.Response(200, json={"id": "p1"})

    gateway = _gateway(httpx.MockTransport(handle))

    await gateway.get_property("p1", cookie_header="sessionId=abc")


async def test_get_property_404_raises_backend_problem_error_with_original_code() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "detail": "No such property."})

    gateway = _gateway(httpx.MockTransport(handle))

    with pytest.raises(BackendProblemError) as exc_info:
        await gateway.get_property("unknown")

    assert exc_info.value.status == 404
    assert exc_info.value.code == "NOT_FOUND"
    assert exc_info.value.detail == "No such property."


async def test_list_room_types_returns_the_parsed_array() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/properties/p1/room-types"
        return httpx.Response(200, json=[{"id": "rt1", "code": "KING"}])

    gateway = _gateway(httpx.MockTransport(handle))

    result = await gateway.list_room_types("p1")

    assert result == [{"id": "rt1", "code": "KING"}]


async def test_list_room_types_backend_error_raises_backend_problem_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"code": "INTERNAL_ERROR", "detail": "Something went wrong."}
        )

    gateway = _gateway(httpx.MockTransport(handle))

    with pytest.raises(BackendProblemError) as exc_info:
        await gateway.list_room_types("p1")

    assert exc_info.value.status == 500
    assert exc_info.value.code == "INTERNAL_ERROR"
