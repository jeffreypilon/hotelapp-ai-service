"""Natural language -> `GET /availability` parameters, in one structured-output call.

AI Step 4 (`phased-implementation-plan.md`): `POST /assistant/search` resolves free text into the
exact parameter set `GET /availability` already accepts, then calls that endpoint -- once, or once
per property when none is named. No LangGraph here -- a single structured-output call does not
need a graph, and wrapping it in one would be machinery this step does not use
(ai-enablement-overview.md §7).

This module is the only place in the service permitted to construct the extraction prompt or call
a model for it, per module-registry.md's "`services/` is the only layer permitted to construct a
prompt or call a model" rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hotelapp_ai.domain.errors import (
    BackendProblemError,
    ProviderUnavailableError,
    SearchParamsIncompleteError,
)
from hotelapp_ai.gateways.hotelapp import AvailabilityParams, HotelAppGateway
from hotelapp_ai.services.llm_client import LLMClient

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Fixed reference data, per shared/migrations/V001__initial_schema.sql and data-model.md --
# constrained in the extraction schema itself so an invalid value is structurally impossible,
# per coding-standards.md#structured-output-over-parsing-prose. Property ids are the opposite:
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

# Per-call ceiling, ai-enablement-overview.md §11: the nano tier, classification-shaped --
# extracting a fixed parameter set plus one short sentence, not prose generation.
EXTRACTION_MODEL_MAX_TOKENS = 400
MODEL_CALL_TIMEOUT_SECONDS = 10.0

# `GET /availability`'s own default. Used to recompute pagination over the merged set when no
# property is named -- see `_merge_availability` below.
DEFAULT_PAGE_SIZE = 20

# Sized to "this chain has exactly two properties to check" (api-contracts.md's Design Decision),
# not to scale: large enough that a single backend call returns every room type a property has,
# so the merge step never silently drops a row to its own per-call page size.
FAN_OUT_PAGE_SIZE = 100


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _render_properties(properties: list[dict[str, Any]]) -> str:
    if not properties:
        return "(no properties in the catalogue)"
    lines = []
    for property_ in properties:
        address = property_.get("address") or {}
        city = address.get("city", "(unknown city)")
        lines.append(f"- id={property_['id']}, name={property_['name']!r}, city={city!r}")
    return "\n".join(lines)


def _extraction_schema(property_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "interpretation": {"type": "string"},
            # `None` is a legal schema enum member alongside the real ids -- OpenAI's strict
            # json_schema mode accepts it as the `null` literal for a `["string", "null"]` field.
            "propertyId": {"type": ["string", "null"], "enum": [*property_ids, None]},
            "checkInDate": {"type": ["string", "null"]},
            "checkOutDate": {"type": ["string", "null"]},
            "numGuests": {"type": ["integer", "null"]},
            "roomTypeCode": {
                "type": "array",
                "items": {"type": "string", "enum": ROOM_TYPE_CODES},
            },
            "rateCategory": {"type": "string", "enum": RATE_CATEGORIES},
            "accessibleOnly": {"type": "boolean"},
            "amenityCode": {
                "type": "array",
                "items": {"type": "string", "enum": AMENITY_CODES},
            },
            "minNightlyRate": {"type": ["string", "null"]},
            "maxNightlyRate": {"type": ["string", "null"]},
        },
        "required": [
            "interpretation",
            "propertyId",
            "checkInDate",
            "checkOutDate",
            "numGuests",
            "roomTypeCode",
            "rateCategory",
            "accessibleOnly",
            "amenityCode",
            "minNightlyRate",
            "maxNightlyRate",
        ],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class SearchDependencies:
    """Everything one `POST /assistant/search` request needs, bound once at app startup."""

    gateway: HotelAppGateway
    llm_client: LLMClient | None
    extraction_model: str


@dataclass(frozen=True)
class SearchResult:
    interpretation: str
    parameters: dict[str, Any]
    results: dict[str, Any]


def _base_availability_params(
    extracted: dict[str, Any], *, property_id: str | None
) -> dict[str, Any]:
    """The parameter set shown to the guest in `parameters`, and the one sent to
    `GET /availability` -- identical either way except for a fan-out call's own `pageSize`
    override, per `_availability_query_params` below.
    """
    params: dict[str, Any] = {
        "propertyId": property_id,
        "checkInDate": extracted["checkInDate"],
        "checkOutDate": extracted["checkOutDate"],
        # Not invented the way a date would be -- `GET /availability` requires a guest count, and
        # one guest is the ordinary unstated default for a search, unlike a date for which there
        # is no sensible default at all.
        "numGuests": extracted["numGuests"] or 1,
    }
    if extracted["roomTypeCode"]:
        params["roomTypeCode"] = extracted["roomTypeCode"]
    if extracted["rateCategory"] and extracted["rateCategory"] != "NONE":
        params["rateCategory"] = extracted["rateCategory"]
    if extracted["accessibleOnly"]:
        params["accessibleOnly"] = True
    if extracted["amenityCode"]:
        params["amenityCode"] = extracted["amenityCode"]
    if extracted["minNightlyRate"]:
        params["minNightlyRate"] = extracted["minNightlyRate"]
    if extracted["maxNightlyRate"]:
        params["maxNightlyRate"] = extracted["maxNightlyRate"]
    return params


def _availability_query_params(
    extracted: dict[str, Any], *, property_id: str, fan_out: bool
) -> AvailabilityParams:
    params: AvailabilityParams = _base_availability_params(extracted, property_id=property_id)
    if fan_out:
        params["pageSize"] = FAN_OUT_PAGE_SIZE
    return params


async def _call_availability(
    gateway: HotelAppGateway, params: AvailabilityParams, *, cookie_header: str | None
) -> dict[str, Any]:
    response = await gateway.get_availability(params, cookie_header=cookie_header)
    if response.status_code >= 400:
        body = response.json()
        raise BackendProblemError(
            status=response.status_code,
            code=body.get("code", "INTERNAL_ERROR"),
            detail=body.get("detail", "GET /availability returned an error."),
        )
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


async def search(
    deps: SearchDependencies, *, query: str, cookie_header: str | None = None
) -> SearchResult:
    """Resolves `query` into `GET /availability` parameters and runs that search -- AI Step 4's
    `POST /assistant/search`. Every failure here is pre-stream (this endpoint is not SSE), so a
    raised exception is the whole error-reporting mechanism; `transport/rest/search.py` maps each
    one to a Problem Details response.
    """
    if deps.llm_client is None:
        raise ProviderUnavailableError("No OPENAI_API_KEY is configured.")

    properties = await deps.gateway.list_properties()
    property_ids = [str(property_["id"]) for property_ in properties]

    today = datetime.now(UTC).date()
    # "Next weekend" requires knowing what day of the week today *is*, not just its calendar
    # date -- found by hand verifying this step: the nano model did not reliably derive the
    # weekday from an ISO date on its own, and returned null dates rather than guess. Spelling
    # the weekday out removes that arithmetic from the model entirely.
    today_rendered = f"{today.isoformat()} ({today.strftime('%A')})"
    prompt = _load_prompt("extract_search_params.md")
    prompt = prompt.replace("<<<TODAY>>>", today_rendered)
    prompt = prompt.replace("<<<PROPERTIES>>>", _render_properties(properties))
    prompt = prompt.replace("<<<QUERY>>>", query)

    extracted = await deps.llm_client.classify_json(
        model=deps.extraction_model,
        system_prompt="Follow the instruction section of the supplied prompt exactly.",
        user_prompt=prompt,
        schema_name="availability_search_params",
        schema=_extraction_schema(property_ids),
        max_tokens=EXTRACTION_MODEL_MAX_TOKENS,
        timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
    )

    if not extracted["checkInDate"] or not extracted["checkOutDate"]:
        raise SearchParamsIncompleteError(
            "I didn't catch your dates \u2014 what check-in and check-out are you thinking?"
        )

    resolved_property_id = extracted["propertyId"]
    if resolved_property_id is not None:
        params = _availability_query_params(
            extracted, property_id=resolved_property_id, fan_out=False
        )
        results = await _call_availability(deps.gateway, params, cookie_header=cookie_header)
    else:
        per_property_results = []
        for property_id in property_ids:
            params = _availability_query_params(extracted, property_id=property_id, fan_out=True)
            per_property_results.append(
                await _call_availability(deps.gateway, params, cookie_header=cookie_header)
            )
        results = _merge_availability(per_property_results)

    return SearchResult(
        interpretation=str(extracted["interpretation"]),
        parameters=_base_availability_params(extracted, property_id=resolved_property_id),
        results=results,
    )
