"""RFC 9457 Problem Details serialization. The only module that builds a Problem Details body --
see module-registry.md's `rest/problem.py` entry.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)

_PROBLEM_BASE_URI = "https://hotelapp.example/problems"
MEDIA_TYPE = "application/problem+json"


def _default_title(code: str) -> str:
    return code.replace("_", " ").capitalize()


def build_problem_body(
    *, path: str, status: int, code: str, detail: str, title: str | None = None
) -> dict[str, Any]:
    slug = code.lower().replace("_", "-")
    return {
        "type": f"{_PROBLEM_BASE_URI}/{slug}",
        "title": title or _default_title(code),
        "status": status,
        "detail": detail,
        "instance": path,
        "code": code,
        "traceId": str(uuid.uuid4()),
    }


def problem_response(
    *, request: Request, status: int, code: str, detail: str, title: str | None = None
) -> JSONResponse:
    body = build_problem_body(
        path=request.url.path, status=status, code=code, detail=detail, title=title
    )
    return JSONResponse(status_code=status, content=body, media_type=MEDIA_TYPE)


class ProblemDetailError(Exception):
    """Raise from any layer to produce a specific RFC 9457 body instead of the generic 500."""

    def __init__(self, *, status: int, code: str, detail: str, title: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title


async def problem_detail_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemDetailError)
    response = problem_response(
        request=request, status=exc.status, code=exc.code, detail=exc.detail, title=exc.title
    )
    logger.warning("problem_detail", code=exc.code, status=exc.status)
    return response


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The global fallback. Logs the real exception; never leaks it to the client."""
    logger.error("unhandled_exception", path=request.url.path, error=str(exc))
    return problem_response(
        request=request,
        status=500,
        code="INTERNAL_ERROR",
        detail="An unexpected error occurred.",
        title="Internal server error",
    )
