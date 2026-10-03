"""Natural language -> `GET /availability` parameters, in one structured-output call.

AI Step 4 (`phased-implementation-plan.md`): `POST /assistant/search` resolves free text into the
exact parameter set `GET /availability` already accepts, then calls that endpoint -- once, or once
per property when none is named. No LangGraph here -- a single structured-output call does not
need a graph, and wrapping it in one would be machinery this step does not use
(ai-enablement-overview.md §7).

This module is the only place in the service permitted to construct the extraction prompt or call
a model for it, per module-registry.md's "`services/` is the only layer permitted to construct a
prompt or call a model" rule. The fan-out/merge logic that needs no model call lives in
`services/availability.py` instead -- shared with the `search_availability` MCP tool (AI Step 5),
which calls that module directly rather than this one so that reaching it from `transport/mcp/`
never also reaches `services/llm_client.py`'s `httpx` import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotelapp_ai.domain.errors import ProviderUnavailableError, SearchParamsIncompleteError
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.services.availability import (
    AMENITY_CODES,
    RATE_CATEGORIES,
    ROOM_TYPE_CODES,
    resolve_availability,
)
from hotelapp_ai.services.llm_client import LLMClient

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Per-call ceiling, ai-enablement-overview.md §11: the nano tier, classification-shaped --
# extracting a fixed parameter set plus one short sentence, not prose generation.
EXTRACTION_MODEL_MAX_TOKENS = 400
MODEL_CALL_TIMEOUT_SECONDS = 10.0


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


def _base_availability_params(extracted: dict[str, Any]) -> dict[str, Any]:
    """The parameter set shown to the guest in `parameters`, and the one sent to
    `GET /availability` -- identical either way except for `propertyId` itself and a fan-out
    call's own `pageSize` override, both added by `resolve_availability` below, not here.
    """
    params: dict[str, Any] = {
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
    base_params = _base_availability_params(extracted)
    results = await resolve_availability(
        deps.gateway,
        base_params=base_params,
        property_id=resolved_property_id,
        cookie_header=cookie_header,
        properties=properties,
    )

    return SearchResult(
        interpretation=str(extracted["interpretation"]),
        parameters={"propertyId": resolved_property_id, **base_params},
        results=results,
    )
