"""Every MCP tool definition and its JSON schema -- the only module permitted to define one, per
module-registry.md's `transport/` table: "Imported by both transports; a tool defined anywhere
else is a defect."

AI Step 5 (`phased-implementation-plan.md`) builds the five **public** tools only --
`list_properties`, `get_property`, `list_room_types`, `search_availability`, `prepare_booking` --
per ai-enablement-overview.md §6's tool-surface table. The four OAuth-gated tools
(`list_reservations`, `get_reservation`, `modify_reservation`, `cancel_reservation`) are a later
step and are deliberately not stubbed here.

**Tag-based filtering, and why there is none.** The step's own drafting anticipated a `fastmcp`
3.0+ release with `include_tags`/`exclude_tags` filtering or a `server.disable(tags=...)` method.
What this project actually resolved is `fastmcp==2.1.2`, whose `FastMCP` class has neither --
`dir(FastMCP)` carries no `disable`, `enable`, `remove_tool`, or tag-filtering method at all, and
`fastmcp.settings.Settings` has no tag-shaped field either (verified against the installed
package, not assumed from memory, per this step's standing instructions). So each transport
registers only the tools it is allowed to expose, by calling the matching `register_*` function
below with its own `FastMCP` instance, rather than registering everything once and filtering
afterwards. The `tags={"public"}` passed to `@mcp.tool` is still a real, checkable fact --
`tests/unit/test_mcp_tools.py` asserts on it -- and is what a future `http.py`'s own
`register_full_surface` would combine with the OAuth-gated tools' own tags.

**This module imports `services/availability.py`, never `services/search.py`.** The latter also
imports `services/llm_client.py` for its own extraction call, which imports `httpx` -- reaching it
from here would trip the same "transport may not reach `httpx`" import-linter contract that
`gateways/hotelapp.py`'s own, separately-justified exception already carves a narrow hole in (see
this repository's `pyproject.toml`). `services/availability.py` makes no model call at all, so no
further exception is needed for it.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.services.availability import AMENITY_CODES, RATE_CATEGORIES, ROOM_TYPE_CODES
from hotelapp_ai.services.availability import resolve_availability as _resolve_availability

RoomTypeCode = Literal["SINGLE", "DOUBLE", "KING", "SUITE", "CONFERENCE_ROOM"]
RateCategory = Literal[
    "NONE",
    "AAA_CAA",
    "AARP",
    "GOVERNMENT_PER_DIEM",
    "MILITARY_VETERAN",
    "SENIOR",
    "CORPORATE_CODE",
    "GROUP_CODE",
]
AmenityCode = Literal[
    "WIFI", "AIR_CONDITIONING", "REFRIGERATOR", "TELEVISION", "MICROWAVE", "WET_BAR", "SAFE"
]

# `services/search.py`'s lists are the one place these values are authored; a `Literal` cannot be
# built from a runtime list, so this assertion is what keeps a future addition there from quietly
# going unrepresented in the MCP schema above.
assert set(RoomTypeCode.__args__) == set(ROOM_TYPE_CODES)  # type: ignore[attr-defined]
assert set(RateCategory.__args__) == set(RATE_CATEGORIES)  # type: ignore[attr-defined]
assert set(AmenityCode.__args__) == set(AMENITY_CODES)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ToolDependencies:
    """Bound once per transport at startup. `stdio.py` builds these for the public-only surface;
    a future `http.py` would additionally carry the caller's own session for the OAuth-gated
    tools, not built in this step.
    """

    gateway: HotelAppGateway
    frontend_base_url: str


def register_public_tools(mcp: FastMCP, deps: ToolDependencies) -> None:
    """Registers the five public tools onto `mcp`, each tagged `public`. No tool here makes a
    model call of its own -- every one is a typed, authorized translation layer over the REST
    contract, per ai-enablement-overview.md §6's "no evaluation-suite gate" note.
    """

    @mcp.tool(tags={"public"})
    async def list_properties() -> list[dict[str, Any]]:
        """Lists every active HotelApp property in the catalogue."""
        return await deps.gateway.list_properties()

    @mcp.tool(tags={"public"})
    async def get_property(
        propertyId: Annotated[str, Field(description="A property's id or slug.")],
    ) -> dict[str, Any]:
        """Fetches one property, including its room types in summary form."""
        return await deps.gateway.get_property(propertyId)

    @mcp.tool(tags={"public"})
    async def list_room_types(
        propertyId: Annotated[str, Field(description="A property's id or slug.")],
    ) -> list[dict[str, Any]]:
        """Lists the active room types at one property, in full detail."""
        return await deps.gateway.list_room_types(propertyId)

    @mcp.tool(tags={"public"})
    async def search_availability(
        checkInDate: Annotated[str, Field(description="YYYY-MM-DD, not in the past.")],
        checkOutDate: Annotated[str, Field(description="YYYY-MM-DD, after checkInDate.")],
        numGuests: Annotated[int, Field(ge=1, description="Guests to accommodate.")] = 1,
        propertyId: Annotated[
            str | None,
            Field(
                description="Restrict to one property's id or slug. Every property in the "
                "catalogue is searched, and the results merged, when omitted."
            ),
        ] = None,
        roomTypeCode: Annotated[
            list[RoomTypeCode] | None, Field(description="Restrict to these room types.")
        ] = None,
        amenityCode: Annotated[
            list[AmenityCode] | None,
            Field(description="A room type must have every amenity listed."),
        ] = None,
        rateCategory: Annotated[
            RateCategory, Field(description="A special rate category.")
        ] = "NONE",
        accessibleOnly: Annotated[
            bool, Field(description="Restrict to accessible room types.")
        ] = False,
        minNightlyRate: Annotated[
            str | None, Field(description="Minimum discounted nightly rate.")
        ] = None,
        maxNightlyRate: Annotated[
            str | None, Field(description="Maximum discounted nightly rate.")
        ] = None,
    ) -> dict[str, Any]:
        """Finds room types with at least one free unit for the whole date range. Mirrors
        `GET /availability`'s own parameters verbatim: the caller -- an MCP client that has
        already turned the guest's natural language into these typed arguments -- is doing the
        extraction F2 does for the REST path, so this tool makes no model call of its own.
        """
        base_params: dict[str, Any] = {
            "checkInDate": checkInDate,
            "checkOutDate": checkOutDate,
            "numGuests": numGuests,
        }
        if roomTypeCode:
            base_params["roomTypeCode"] = roomTypeCode
        if rateCategory != "NONE":
            base_params["rateCategory"] = rateCategory
        if accessibleOnly:
            base_params["accessibleOnly"] = True
        if amenityCode:
            base_params["amenityCode"] = amenityCode
        if minNightlyRate:
            base_params["minNightlyRate"] = minNightlyRate
        if maxNightlyRate:
            base_params["maxNightlyRate"] = maxNightlyRate

        return await _resolve_availability(
            deps.gateway, base_params=base_params, property_id=propertyId
        )

    @mcp.tool(tags={"public"})
    async def prepare_booking(
        propertyId: Annotated[str, Field(description="The property's id or slug.")],
        roomTypeCode: Annotated[RoomTypeCode, Field(description="The one room type to offer.")],
        checkInDate: Annotated[str, Field(description="YYYY-MM-DD.")],
        checkOutDate: Annotated[str, Field(description="YYYY-MM-DD.")],
        numGuests: Annotated[int, Field(ge=1, description="Guests to accommodate.")] = 1,
    ) -> dict[str, str]:
        """Returns a deep link into the guest search-results screen, pre-filled with this search.
        **Creates no reservation and performs no write of any kind** -- the guest completes
        payment in the first-party UI; see ai-enablement-overview.md §6's Design Decision for why
        the agent prepares bookings rather than paying for them, and why the link targets the
        search screen rather than the booking summary screen.
        """
        query = urllib.parse.urlencode(
            {
                "checkInDate": checkInDate,
                "checkOutDate": checkOutDate,
                "numGuests": numGuests,
                "roomTypeCode": roomTypeCode,
            }
        )
        base_url = deps.frontend_base_url.rstrip("/")
        return {"url": f"{base_url}/properties/{propertyId}/search?{query}"}
