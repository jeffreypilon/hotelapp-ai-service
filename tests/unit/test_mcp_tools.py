"""Unit tests for `transport/mcp/tools.py` -- AI Step 5's public MCP tool surface.

Connects the official MCP Python SDK's own `ClientSession` to the exact `FastMCP` server object
`transport/mcp/stdio.py` would run, over `mcp.shared.memory`'s in-process transport rather than a
spawned OS subprocess -- the wire protocol and tool-dispatch code are identical either way; only
the byte-stream carrier differs, and a subprocess buys nothing here but slower, more fragile
tests. `HotelAppGateway` is a hand-written fake, the same pattern `tests/unit/test_search.py` uses
-- no real backend, no real provider, matching testing-standards.md's "Tool schemas,
allow-listing, pre-dispatch validation" deterministic-and-asserted-exactly row.

The hand-verified pass against a real Claude Desktop session -- this step's actual demo moment,
per `phased-implementation-plan.md`'s "Verify" section -- is not something an automated suite can
substitute for, and is not attempted here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult

from hotelapp_ai.transport.mcp.tools import ToolDependencies, register_public_tools

_HARBORVIEW_ID = "0192f3a2-1100-7000-8000-000000000001"
_LAKESIDE_ID = "0192f3a2-1100-7000-8000-000000000002"

_PROPERTIES = [
    {"id": _HARBORVIEW_ID, "name": "Harborview Grand", "address": {"city": "Portland"}},
    {"id": _LAKESIDE_ID, "name": "Lakeside Inn", "address": {"city": "Burlington"}},
]


def _availability_body(*, room_code: str, nightly_rate: str, room_id: str) -> dict[str, Any]:
    return {
        "data": [
            {
                "roomType": {"id": room_id, "code": room_code, "name": room_code.title()},
                "pricing": {"nightlyRate": nightly_rate, "currency": "USD"},
                "availableRoomCount": 2,
            }
        ],
        "pagination": {
            "page": 1,
            "pageSize": 20,
            "totalItems": 1,
            "totalPages": 1,
            "hasPreviousPage": False,
            "hasNextPage": False,
        },
    }


@dataclass
class _FakeGateway:
    """Fakes `HotelAppGateway` -- no real backend, matching `tests/unit/test_search.py`'s own
    fake exactly, plus the two methods AI Step 5 added.
    """

    availability_by_property: dict[str, dict[str, Any]]
    properties_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    room_types_by_property: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    last_cookie_header: str | None = field(default=None, init=False)

    async def list_properties(self) -> list[dict[str, Any]]:
        return _PROPERTIES

    async def get_property(self, property_id: str, *, cookie_header: str | None = None) -> Any:
        self.last_cookie_header = cookie_header
        return self.properties_by_id[property_id]

    async def list_room_types(
        self, property_id: str, *, cookie_header: str | None = None
    ) -> list[dict[str, Any]]:
        self.last_cookie_header = cookie_header
        return self.room_types_by_property[property_id]

    async def get_availability(
        self, params: dict[str, Any], *, cookie_header: str | None = None
    ) -> httpx.Response:
        self.calls.append(dict(params))
        return httpx.Response(200, json=self.availability_by_property[params["propertyId"]])


def _build_server(gateway: _FakeGateway) -> FastMCP:
    mcp = FastMCP("hotelapp-test")
    deps = ToolDependencies(gateway=gateway, frontend_base_url="http://localhost:5173")  # type: ignore[arg-type]
    register_public_tools(mcp, deps)
    return mcp


def _result_json(result: CallToolResult) -> Any:
    assert not result.isError, result.content
    [content] = result.content
    assert content.type == "text"
    return json.loads(content.text)


async def test_exactly_five_public_tools_are_exposed() -> None:
    """The allow-listing assertion this step's own instructions call for: no reservation or
    write tool leaks onto the public surface.
    """
    gateway = _FakeGateway(availability_by_property={})
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        tools = (await session.list_tools()).tools

    assert {tool.name for tool in tools} == {
        "list_properties",
        "get_property",
        "list_room_types",
        "search_availability",
        "prepare_booking",
    }


async def test_list_properties_returns_the_catalogue() -> None:
    gateway = _FakeGateway(availability_by_property={})
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool("list_properties", {})

    assert _result_json(result) == _PROPERTIES


async def test_get_property_calls_the_gateway_with_the_given_id() -> None:
    gateway = _FakeGateway(
        availability_by_property={},
        properties_by_id={_HARBORVIEW_ID: {"id": _HARBORVIEW_ID, "name": "Harborview Grand"}},
    )
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool("get_property", {"propertyId": _HARBORVIEW_ID})

    assert _result_json(result) == {"id": _HARBORVIEW_ID, "name": "Harborview Grand"}


async def test_list_room_types_calls_the_gateway_with_the_given_id() -> None:
    gateway = _FakeGateway(
        availability_by_property={},
        room_types_by_property={_HARBORVIEW_ID: [{"id": "rt1", "code": "KING"}]},
    )
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool("list_room_types", {"propertyId": _HARBORVIEW_ID})

    assert _result_json(result) == [{"id": "rt1", "code": "KING"}]


async def test_search_availability_with_a_property_id_makes_one_call() -> None:
    gateway = _FakeGateway(
        availability_by_property={
            _HARBORVIEW_ID: _availability_body(
                room_code="KING", nightly_rate="224.10", room_id="room-king"
            )
        }
    )
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool(
            "search_availability",
            {
                "checkInDate": "2026-11-14",
                "checkOutDate": "2026-11-16",
                "numGuests": 2,
                "propertyId": _HARBORVIEW_ID,
            },
        )

    body = _result_json(result)
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["propertyId"] == _HARBORVIEW_ID
    assert "pageSize" not in gateway.calls[0]
    assert body["data"][0]["roomType"]["code"] == "KING"


async def test_search_availability_without_a_property_id_fans_out_and_merges() -> None:
    gateway = _FakeGateway(
        availability_by_property={
            _HARBORVIEW_ID: _availability_body(
                room_code="KING", nightly_rate="300.00", room_id="room-king"
            ),
            _LAKESIDE_ID: _availability_body(
                room_code="DOUBLE", nightly_rate="150.00", room_id="room-double"
            ),
        }
    )
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool(
            "search_availability",
            {"checkInDate": "2026-11-14", "checkOutDate": "2026-11-16", "numGuests": 1},
        )

    body = _result_json(result)
    assert {call["propertyId"] for call in gateway.calls} == {_HARBORVIEW_ID, _LAKESIDE_ID}
    assert all(call["pageSize"] == 100 for call in gateway.calls)
    assert [row["roomType"]["code"] for row in body["data"]] == ["DOUBLE", "KING"]


async def test_prepare_booking_returns_a_deep_link_into_s3_with_no_reservation_created() -> None:
    gateway = _FakeGateway(availability_by_property={})
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        result = await session.call_tool(
            "prepare_booking",
            {
                "propertyId": _HARBORVIEW_ID,
                "roomTypeCode": "KING",
                "checkInDate": "2026-11-14",
                "checkOutDate": "2026-11-16",
                "numGuests": 2,
            },
        )

    body = _result_json(result)
    url = body["url"]
    assert url.startswith(f"http://localhost:5173/properties/{_HARBORVIEW_ID}/search?")
    assert "checkInDate=2026-11-14" in url
    assert "checkOutDate=2026-11-16" in url
    assert "numGuests=2" in url
    assert "roomTypeCode=KING" in url


@pytest.mark.parametrize(
    "tool_name",
    ["list_reservations", "get_reservation", "modify_reservation", "cancel_reservation"],
)
async def test_oauth_gated_tools_are_not_registered(tool_name: str) -> None:
    """The four OAuth-gated tools are a later step and must never appear on this surface --
    ai-enablement-overview.md §6's "stdio is deliberately limited to the anonymous surface".
    """
    gateway = _FakeGateway(availability_by_property={})
    mcp = _build_server(gateway)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:  # noqa: SLF001
        tools = (await session.list_tools()).tools

    assert tool_name not in {tool.name for tool in tools}
