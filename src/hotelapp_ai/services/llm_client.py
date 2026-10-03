"""Raw HTTP client for OpenAI chat completions: generation, grading, and query rewrite.

Mirrors `services/ingestion.py`'s `OpenAIEmbeddingProvider` -- a thin `httpx` wrapper around the
provider's own HTTP API, rather than the `langchain-openai` client, so the one role that already
has a working, tested pattern does not grow a second way to call the same endpoint. `langgraph`
(dependency-policy.md) is added for the retrieval graph itself, in `services/assistant.py`;
nothing in this module depends on it.

Every call is bounded -- explicit `max_tokens`, explicit timeout, per
coding-standards.md#every-model-call-is-bounded -- and every provider failure is raised as one of
`domain.errors`' exceptions, never as a raw provider error reaching a caller.
`transport/rest/assistant.py` maps each to the problem code in
error-handling.md#1-new-problem-codes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from hotelapp_ai.domain.errors import (
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

# $/1M tokens, OpenAI list pricing as of this writing. Used only to report cost on the `done`
# event and in the request log line -- never to make a routing decision.
_PROMPT_PRICE_PER_MILLION = {
    "gpt-5.4-mini": Decimal("0.25"),
    "gpt-5.4-nano": Decimal("0.05"),
}
_COMPLETION_PRICE_PER_MILLION = {
    "gpt-5.4-mini": Decimal("2.00"),
    "gpt-5.4-nano": Decimal("0.40"),
}


@dataclass(frozen=True)
class GenerationDelta:
    text: str


@dataclass(frozen=True)
class GenerationUsage:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


def _cost_usd(*, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    prompt_price = _PROMPT_PRICE_PER_MILLION.get(model, Decimal("0"))
    completion_price = _COMPLETION_PRICE_PER_MILLION.get(model, Decimal("0"))
    cost = (
        Decimal(prompt_tokens) * prompt_price + Decimal(completion_tokens) * completion_price
    ) / Decimal("1000000")
    return cost.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise ProviderRateLimitedError(f"OpenAI rate-limited this request: {response.text}")
    if response.status_code >= 500:
        raise ProviderUnavailableError(f"OpenAI returned {response.status_code}: {response.text}")
    response.raise_for_status()


class LLMClient:
    """One shared client for every model call this service makes other than embeddings."""

    def __init__(self, *, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_generation(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> AsyncIterator[GenerationDelta | GenerationUsage]:
        """Streams the guest-facing answer token by token, ending with exactly one
        `GenerationUsage`. Raises before yielding anything if the provider cannot be reached at
        all; raises mid-generator on a mid-stream failure, which the caller (`services/
        assistant.py` / `transport/rest/assistant.py`) turns into a terminal SSE `error` event
        rather than a truncated stream.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # OpenAI's newer models reject `max_tokens` outright in favour of
            # `max_completion_tokens` -- found by hand running this against the real API while
            # verifying this step; `max_tokens` remains this module's parameter name since it is
            # the project-wide term everywhere else (coding-standards.md, settings.py).
            "max_completion_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                timeout=httpx.Timeout(timeout_seconds),
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    _raise_for_status(response)

                prompt_tokens = 0
                completion_tokens = 0
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data == "[DONE]":
                        break

                    chunk = json.loads(data)
                    usage = chunk.get("usage")
                    if usage:
                        prompt_tokens = usage["prompt_tokens"]
                        completion_tokens = usage["completion_tokens"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue

                    choice = choices[0]
                    finish_reason = choice.get("finish_reason")
                    if finish_reason == "content_filter":
                        raise ProviderContentFilteredError(
                            "OpenAI declined to continue generating on policy grounds."
                        )

                    delta_text = choice.get("delta", {}).get("content")
                    if delta_text:
                        yield GenerationDelta(text=delta_text)

                yield GenerationUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=_cost_usd(
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    ),
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

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
        """Structured output for a classification-shaped call -- query rewrite and retrieval
        grading, per coding-standards.md#structured-output-over-parsing-prose. Never regexes
        prose for a value.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_completion_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            },
        }
        try:
            response = await self._client.post(
                "/chat/completions",
                json=payload,
                timeout=httpx.Timeout(timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

        _raise_for_status(response)
        body = response.json()
        choice = body["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise ProviderContentFilteredError(
                "OpenAI declined to classify this content on policy grounds."
            )

        content = choice["message"]["content"]
        return dict(json.loads(content))
