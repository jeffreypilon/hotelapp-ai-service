"""`GET /availability` resolution and merge -- the part of F2 that makes no model call.

Split out of `services/search.py` during AI Step 5: `transport/mcp/tools.py`'s
`search_availability` tool needs exactly this logic (one call for a resolved property, or a
fan-out-and-merge across the catalogue when none is named) with **no extraction call of its
own** -- an MCP client has already turned the caller's natural language into typed arguments.
Importing `services/search.py` itself for that would also import `services/llm_client.py` (and
therefore `httpx`, transitively) purely because the two happen to share a module, which is
exactly what `transport`'s import-linter contract exists to prevent -- see this repository's
`pyproject.toml` comment on the `ignore_imports` entry this split made unnecessary for anything
but `gateways/hotelapp.py` itself.

Reference data (`ROOM_TYPE_CODES`, `RATE_CATEGORIES`, `AMENITY_CODES`) lives here too, for the
same reason: both `services/search.py`'s extraction schema and `transport/mcp/tools.py`'s tool
schema constrain against it, and it must exist in exactly one place
(data-model.md, `shared/migrations/V001__initial_schema.sql`).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from hotelapp_ai.gateways.hotelapp import AvailabilityParams, HotelAppGateway, raise_for_problem

# Fixed reference data, per shared/migrations/V001__initial_schema.sql and data-model.md --
# constrained in both schemas that use it so an invalid value is structurally impossible, per
# coding-standards.md#structured-output-over-parsing-prose. Property ids are the opposite:
# resolved against the *live* `GET /properties` list every call, never hardcoded here, because a
# property can be renamed or added -- see api-contracts.md's `POST /assistant/search`.
ROOM_TYPE_CODES = ["SINGLE", "DOUBLE", "KING", "SUITE", "CONFERENCE_ROOM"]
RATE_CATEGORIES = [
    "NONE",
    "AAA_CAA",
    "AARP",
    "GOVERNMENT_PER_DIEM",
    "MILITARY_VETERAN",
    "SENIOR",
    "CORPORATE_CODE",
    "GROUP_CODE",
]
AMENITY_CODES = [
    "WIFI",
    "AIR_CONDITIONING",
    "REFRIGERATOR",
    "TELEVISION",
    "MICROWAVE",
    "WET_BAR",
    "SAFE",
]

# `GET /availability`'s own default. Used to recompute pagination over the merged set when no
# property is named -- see `_merge_availability` below.
DEFAULT_PAGE_SIZE = 20

# Sized to "this chain has exactly two properties to check" (api-contracts.md's Design Decision),
# not to scale: large enough that a single backend call returns every room type a property has,
# so the merge step never silently drops a row to its own per-call page size.
FAN_OUT_PAGE_SIZE = 100


async def _call_availability(
    gateway: HotelAppGateway, params: AvailabilityParams, *, cookie_header: str | None
) -> dict[str, Any]:
    response = await gateway.get_availability(params, cookie_header=cookie_header)
    raise_for_problem(response, default_detail="GET /availability returned an error.")
    result: dict[str, Any] = response.json()
    return result


def _room_type_sort_key(row: dict[str, Any]) -> tuple[Decimal, str]:
    try:
        nightly_rate = Decimal(row["pricing"]["nightlyRate"])
    except (InvalidOperation, KeyError, TypeError):
        nightly_rate = Decimal("0")
    # Stabilized by appending `id` as the final key, per api-contracts.md's pagination section,
    # so the merged page never duplicates or drops a row across repeated runs.
    return (nightly_rate, str(row["roomType"]["id"]))


def _merge_availability(per_property_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Merges `data` across every property call, re-applies `GET /availability`'s own default
    sort (`nightlyRate:asc`), and recomputes `pagination` over the combined set -- the response
    **shape** stays identical to the single-property case, per api-contracts.md's Design
    Decision.
    """
    merged_data = [row for result in per_property_results for row in result["data"]]
    merged_data.sort(key=_room_type_sort_key)

    total_items = len(merged_data)
    total_pages = max(1, math.ceil(total_items / DEFAULT_PAGE_SIZE))
    page_data = merged_data[:DEFAULT_PAGE_SIZE]

    return {
        "data": page_data,
        "pagination": {
            "page": 1,
            "pageSize": DEFAULT_PAGE_SIZE,
            "totalItems": total_items,
            "totalPages": total_pages,
            "hasPreviousPage": False,
            "hasNextPage": total_items > DEFAULT_PAGE_SIZE,
        },
    }


async def resolve_availability(
    gateway: HotelAppGateway,
    *,
    base_params: dict[str, Any],
    property_id: str | None,
    cookie_header: str | None = None,
    properties: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Runs `GET /availability` for one resolved property, or fans out across every property in
    the catalogue and merges the result when none is resolved. The shared half of F2's
    natural-language search (`services/search.py`) and the `search_availability` MCP tool
    (`transport/mcp/tools.py`, AI Step 5) -- they differ only in how `base_params` and
    `property_id` were arrived at: one structured-output extraction call in `services/search.py`,
    a typed tool argument here with no extraction at all. `base_params` carries every
    `GET /availability` parameter except `propertyId` and `pageSize`, both added here.

    `properties` lets a caller that already fetched the catalogue -- `services/search.py`'s
    `search`, for its own extraction prompt -- pass it through rather than this function
    re-fetching it; omitted, it is fetched here, which is what the MCP tool (with no reason to
    have fetched it already) does.
    """
    if property_id is not None:
        params: AvailabilityParams = {**base_params, "propertyId": property_id}
        return await _call_availability(gateway, params, cookie_header=cookie_header)

    if properties is None:
        properties = await gateway.list_properties()
    per_property_results = []
    for property_ in properties:
        params = {
            **base_params,
            "propertyId": str(property_["id"]),
            "pageSize": FAN_OUT_PAGE_SIZE,
        }
        per_property_results.append(
            await _call_availability(gateway, params, cookie_header=cookie_header)
        )
    return _merge_availability(per_property_results)
