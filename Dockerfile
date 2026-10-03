# syntax=docker/dockerfile:1.7

# Phase 9 AI service image. python:3.13-slim matches requires-python in pyproject.toml exactly,
# so a local run and a container run cannot silently differ (dependency-policy.md#packaging).

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

# Downloads the cross-encoder reranker's weights at *build* time, not on first use at runtime.
# A first-use download would break the offline property Phase 8 established -- the whole point of
# baking the model in is that the container never needs network access to rerank a passage
# (dependency-policy.md, AI Step 2). The model name must match RERANKER_MODEL_NAME in
# services/reranking.py; a mismatch here means a cache miss and a silent fallback to downloading.
ENV HF_HOME=/app/.cache/huggingface
RUN .venv/bin/python -c \
    "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

FROM python:3.13-slim
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" HF_HOME=/app/.cache/huggingface HF_HUB_OFFLINE=1
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/.cache ./.cache
COPY --from=build /app/src ./src
EXPOSE 8000
CMD ["hotelapp-ai", "serve"]
