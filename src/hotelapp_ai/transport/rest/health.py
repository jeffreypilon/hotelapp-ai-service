"""`GET /api/v1/assistant/health` -- reports provider, retrieval, and backend reachability.
"the service is running" and "the service can answer questions" are different facts; see
api-contracts.md's `GET /assistant/health`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

router = APIRouter()


class HealthResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str
    provider: str
    retrieval: str
    backend: str
    backend_target: str


def _backend_target_for(base_url: str) -> str:
    if ":8080" in base_url:
        return "springboot"
    if ":3000" in base_url:
        return "nodejs"
    return "unknown"


@router.get("/assistant/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    gateway = request.app.state.gateway

    backend = "UP" if await gateway.check_backend_reachable() else "DOWN"

    return HealthResponse(
        status="UP",
        provider="UP" if settings.provider_configured else "NOT_CONFIGURED",
        retrieval=request.app.state.retrieval_status,
        backend=backend,
        backend_target=_backend_target_for(settings.hotelapp_api_base_url),
    )
