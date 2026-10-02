# syntax=docker/dockerfile:1.7

# Phase 9 AI service image. python:3.13-slim matches requires-python in pyproject.toml exactly,
# so a local run and a container run cannot silently differ (dependency-policy.md#packaging).

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# Same WORKDIR as the final stage -- uv's console-script shims embed an absolute shebang to this
# path's venv interpreter, so the two stages must agree or the copied venv's scripts break.
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/src ./src
EXPOSE 8000
CMD ["hotelapp-ai", "serve"]
