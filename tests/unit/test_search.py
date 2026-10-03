"""Unit tests for `services/search.py` -- AI Step 4's natural-language availability search.

No real backend, no real provider -- `HotelAppGateway` and the LLM client are hand-written fakes,
the same pattern `tests/unit/test_assistant.py` uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from hotelapp_ai.domain.errors import (
    BackendProblemError,
    ProviderUnavailableError,
    SearchParamsIncompleteError,
)
from hotelapp_ai.services.search import SearchDependencies, search

_HARBORVIEW_ID = "0192f3a2-1100-7000-8000-000000000001"
_LAKESIDE_ID = "0192f3a2-1100-7000-8000-000000000002"

_PROPERTIES = [
    {
        "id": _HARBORVIEW_ID,
        "name": "Harborview Grand",
        "address": {"city": "Portland"},
    },
    {
        "id": _LAKESIDE_ID,
        "name": "Lakeside Inn",
        "address": {"city": "Burlington"},
    },
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
class _FakeLLMClient:
    """Returns a scripted extraction result, recording the schema's property-id enum and the
    rendered prompt so a test can assert extraction never saw a hardcoded property name.
    """

    extraction_result: dict[str, Any]
    last_schema: dict[str, Any] | None = field(default=None, init=False)
    last_prompt: str | None = field(default=None, init=False)

    async def classify_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.last_schema = schema
        self.last_prompt = user_prompt
        return self.extraction_result


@dataclass
class _FakeGateway:
    """Fakes `HotelAppGateway`. `availability_by_property` maps a property id to the raw JSON
    body `GET /availability` would return for it; a `None` value raises `404` (unknown property),
    and a missing key is a test author error, not a silently empty result.
    """

    availability_by_property: dict[str, dict[str, Any] | tuple[int, str, str]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_properties(self) -> list[dict[str, Any]]:
        return _PROPERTIES

    async def get_availability(
        self, params: dict[str, Any], *, cookie_header: str | None = None
    ) -> httpx.Response:
        self.calls.append(dict(params))
        outcome = self.availability_by_property[params["propertyId"]]
        if isinstance(outcome, tuple):
            status, code, detail = outcome
            return httpx.Response(status, json={"code": code, "detail": detail})
        return httpx.Response(200, json=outcome)


def _base_extraction(**overrides: Any) -> dict[str, Any]:
    extracted: dict[str, Any] = {
        "interpretation": "2 guests \u00b7 2026-11-14 \u2013 2026-11-16",
        "propertyId": None,
        "checkInDate": "2026-11-14",
        "checkOutDate": "2026-11-16",
        "numGuests": 2,
        "roomTypeCode": [],
        "rateCategory": "NONE",
        "accessibleOnly": False,
        "amenityCode": [],
        "minNightlyRate": None,
        "maxNightlyRate": None,
    }
    extracted.update(overrides)
    return extracted


async def test_named_property_makes_one_call_and_passes_results_through_verbatim() -> None:
    llm_client = _FakeLLMClient(_base_extraction(propertyId=_HARBORVIEW_ID))
    gateway = _FakeGateway(
        {
            _HARBORVIEW_ID: _availability_body(
                room_code="KING", nightly_rate="224.10", room_id="room-king"
            )
        }
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    result = await search(deps, query="a king room at Harborview Grand")

    assert len(gateway.calls) == 1
    assert gateway.calls[0]["propertyId"] == _HARBORVIEW_ID
    assert "pageSize" not in gateway.calls[0]
    assert result.parameters["propertyId"] == _HARBORVIEW_ID
    assert result.parameters["numGuests"] == 2
    assert result.results["data"][0]["roomType"]["code"] == "KING"


async def test_property_ids_offered_to_extraction_are_the_live_catalogue() -> None:
    """The schema's `propertyId` enum must be built from `get_properties()`, never hardcoded --
    api-contracts.md's "resolved against the live GET /properties list" rule.
    """
    llm_client = _FakeLLMClient(_base_extraction(propertyId=_HARBORVIEW_ID))
    gateway = _FakeGateway(
        {_HARBORVIEW_ID: _availability_body(room_code="KING", nightly_rate="224.10", room_id="r1")}
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    await search(deps, query="a king room at Harborview Grand")

    assert llm_client.last_schema is not None
    property_id_schema = llm_client.last_schema["properties"]["propertyId"]
    assert _HARBORVIEW_ID in property_id_schema["enum"]
    assert _LAKESIDE_ID in property_id_schema["enum"]
    assert "Harborview Grand" in (llm_client.last_prompt or "")


async def test_unresolved_property_fans_out_and_merges_both_properties() -> None:
    llm_client = _FakeLLMClient(_base_extraction(propertyId=None))
    gateway = _FakeGateway(
        {
            _HARBORVIEW_ID: _availability_body(
                room_code="KING", nightly_rate="224.10", room_id="harborview-king"
            ),
            _LAKESIDE_ID: _availability_body(
                room_code="DOUBLE", nightly_rate="150.00", room_id="lakeside-double"
            ),
        }
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    result = await search(deps, query="a room for two")

    # One call per property, each requesting the full fan-out page size -- see
    # `FAN_OUT_PAGE_SIZE` in services/search.py.
    assert len(gateway.calls) == 2
    assert {call["propertyId"] for call in gateway.calls} == {_HARBORVIEW_ID, _LAKESIDE_ID}
    assert all(call["pageSize"] == 100 for call in gateway.calls)

    # The merged result genuinely contains rows from both properties, not just "doesn't crash".
    codes = {row["roomType"]["code"] for row in result.results["data"]}
    assert codes == {"KING", "DOUBLE"}
    assert result.parameters["propertyId"] is None

    # Re-sorted by nightlyRate:asc across the combined set -- GET /availability's own default.
    assert [row["roomType"]["code"] for row in result.results["data"]] == ["DOUBLE", "KING"]
    assert result.results["pagination"]["totalItems"] == 2


async def test_missing_dates_raises_before_any_backend_call() -> None:
    llm_client = _FakeLLMClient(
        _base_extraction(checkInDate=None, checkOutDate=None, propertyId=_HARBORVIEW_ID)
    )
    gateway = _FakeGateway(
        {_HARBORVIEW_ID: _availability_body(room_code="KING", nightly_rate="1", room_id="r")}
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    with pytest.raises(SearchParamsIncompleteError) as exc_info:
        await search(deps, query="a quiet room for two")

    assert "dates" in exc_info.value.detail
    assert gateway.calls == []


async def test_backend_validation_error_passes_through_unmodified() -> None:
    llm_client = _FakeLLMClient(_base_extraction(propertyId=_HARBORVIEW_ID))
    gateway = _FakeGateway(
        {_HARBORVIEW_ID: (400, "VALIDATION_FAILED", "checkOutDate must be after checkInDate.")}
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    with pytest.raises(BackendProblemError) as exc_info:
        await search(deps, query="a king room at Harborview Grand")

    assert exc_info.value.status == 400
    assert exc_info.value.code == "VALIDATION_FAILED"
    assert exc_info.value.detail == "checkOutDate must be after checkInDate."


async def test_unconfigured_provider_raises_before_touching_the_gateway() -> None:
    gateway = _FakeGateway({})
    deps = SearchDependencies(gateway=gateway, llm_client=None, extraction_model="nano")

    with pytest.raises(ProviderUnavailableError):
        await search(deps, query="a king room at Harborview Grand")

    assert gateway.calls == []


async def test_numGuests_defaults_to_one_when_not_mentioned() -> None:
    llm_client = _FakeLLMClient(_base_extraction(propertyId=_HARBORVIEW_ID, numGuests=None))
    gateway = _FakeGateway(
        {_HARBORVIEW_ID: _availability_body(room_code="KING", nightly_rate="224.10", room_id="r1")}
    )
    deps = SearchDependencies(gateway=gateway, llm_client=llm_client, extraction_model="nano")  # type: ignore[arg-type]

    result = await search(deps, query="a king room at Harborview Grand next week")

    assert result.parameters["numGuests"] == 1
    assert gateway.calls[0]["numGuests"] == 1
