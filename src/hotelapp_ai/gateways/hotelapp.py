"""The ONLY module that may call a HotelApp backend.

Forwards the caller's session cookie verbatim and adds no credentials of its own -- there is no
code path here that could. See architecture-specification.md's "Calling the backends" and
module-registry.md's `gateways/` entry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from hotelapp_ai.domain.errors import BackendProblemError

# The subset of httpx's own `QueryParamTypes` this gateway actually needs -- a scalar per
# parameter, or a list for a repeatable one (`roomTypeCode`, `amenityCode`), matching
# `GET /availability`'s own query string shape.
AvailabilityParams = dict[str, "str | int | float | bool | list[str] | None"]


def raise_for_problem(response: httpx.Response, *, default_detail: str) -> None:
    """Translates a `4xx`/`5xx` HotelApp response into `BackendProblemError`, carrying the
    original `code` through unmodified rather than a paraphrase --
    architecture-specification.md's "Calling the backends" section. Shared by every gateway
    method that needs more than `services/search.py`'s own `_call_availability` already had, so
    this reasoning exists in exactly one place.
    """
    if response.status_code >= 400:
        body = response.json()
        raise BackendProblemError(
            status=response.status_code,
            code=body.get("code", "INTERNAL_ERROR"),
            detail=body.get("detail", default_detail),
        )


class HotelAppGateway:
    """Thin async wrapper around one shared `httpx.AsyncClient`."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    @classmethod
    @asynccontextmanager
    async def open(
        cls, base_url: str, *, timeout_seconds: float = 10.0
    ) -> AsyncIterator[HotelAppGateway]:
        """Owns its own `httpx.AsyncClient` for a caller with no app-lifespan to bind one to --
        `transport/mcp/stdio.py` (AI Step 5), unlike `main.py`'s FastAPI lifespan, which builds
        and closes one itself and passes it to `__init__` directly. Keeping client construction
        here, rather than in `stdio.py`, is what lets that module avoid importing `httpx` at all,
        which would otherwise need its own exception in this repository's import-linter
        contract alongside this module's.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
            yield cls(client, base_url)

    async def get_properties(self, *, cookie_header: str | None = None) -> httpx.Response:
        """`GET /properties` -- the public catalogue. Used by the walking skeleton and by the
        health check to prove backend reachability without needing a session.
        """
        headers = {"Cookie": cookie_header} if cookie_header else None
        return await self._client.get(f"{self._base_url}/properties", headers=headers)

    async def get_availability(
        self, params: AvailabilityParams, *, cookie_header: str | None = None
    ) -> httpx.Response:
        """`GET /availability` -- the public availability search. Used by `services/search.py`,
        once per resolved property or once per property in the catalogue when none is named.
        Returns the raw response; interpreting the body -- including Problem Details passthrough
        -- is the caller's job, exactly as `get_properties` leaves it to `check_backend_reachable`
        and `list_properties`.
        """
        headers = {"Cookie": cookie_header} if cookie_header else None
        return await self._client.get(
            f"{self._base_url}/availability", params=params, headers=headers
        )

    async def list_properties(self) -> list[dict[str, object]]:
        response = await self.get_properties()
        response.raise_for_status()
        payload = response.json()
        return list(payload["data"])

    async def get_property(
        self, property_id: str, *, cookie_header: str | None = None
    ) -> dict[str, Any]:
        """`GET /properties/{propertyId}` -- one property plus its room types in summary form.
        Accepts a UUID or a slug, exactly as the endpoint does. Used by the `get_property` MCP
        tool (AI Step 5).
        """
        headers = {"Cookie": cookie_header} if cookie_header else None
        response = await self._client.get(
            f"{self._base_url}/properties/{property_id}", headers=headers
        )
        raise_for_problem(
            response, default_detail="GET /properties/{propertyId} returned an error."
        )
        result: dict[str, Any] = response.json()
        return result

    async def list_room_types(
        self, property_id: str, *, cookie_header: str | None = None
    ) -> list[dict[str, Any]]:
        """`GET /properties/{propertyId}/room-types` -- non-paginated, full detail (unlike
        `GET /availability`'s summary form). Used by the `list_room_types` MCP tool
        (AI Step 5).
        """
        headers = {"Cookie": cookie_header} if cookie_header else None
        response = await self._client.get(
            f"{self._base_url}/properties/{property_id}/room-types", headers=headers
        )
        raise_for_problem(
            response, default_detail="GET /properties/{propertyId}/room-types returned an error."
        )
        return list(response.json())

    async def check_backend_reachable(self) -> bool:
        """Used by `transport/rest/health.py`, which may never import `httpx` itself --
        catching and classifying the request error belongs to this layer, not transport's.
        """
        try:
            response = await self.get_properties()
        except httpx.RequestError:
            return False
        return response.status_code < 500
