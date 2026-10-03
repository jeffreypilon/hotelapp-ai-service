"""The ONLY module that may call a HotelApp backend.

Forwards the caller's session cookie verbatim and adds no credentials of its own -- there is no
code path here that could. See architecture-specification.md's "Calling the backends" and
module-registry.md's `gateways/` entry.
"""

from __future__ import annotations

import httpx

# The subset of httpx's own `QueryParamTypes` this gateway actually needs -- a scalar per
# parameter, or a list for a repeatable one (`roomTypeCode`, `amenityCode`), matching
# `GET /availability`'s own query string shape.
AvailabilityParams = dict[str, "str | int | float | bool | list[str] | None"]


class HotelAppGateway:
    """Thin async wrapper around one shared `httpx.AsyncClient`."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

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

    async def check_backend_reachable(self) -> bool:
        """Used by `transport/rest/health.py`, which may never import `httpx` itself --
        catching and classifying the request error belongs to this layer, not transport's.
        """
        try:
            response = await self.get_properties()
        except httpx.RequestError:
            return False
        return response.status_code < 500
