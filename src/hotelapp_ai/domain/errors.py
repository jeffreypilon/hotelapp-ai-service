"""Provider and retrieval failure types shared across `services/` and `transport/`.

Pure exception classes, no I/O -- living in `domain/` (stdlib only) is what lets
`transport/rest/assistant.py` match on these types for the pre-stream Problem Details mapping
without transitively importing `httpx` or `psycopg`, which `services/llm_client.py` and
`services/assistant.py` need for the real work. See error-handling.md §1 for the problem code each
one maps to.
"""

from __future__ import annotations


class QuestionTooLongError(Exception):
    """Input exceeded `Settings.question_max_length`. Checked before any model call --
    `QUESTION_TOO_LONG`, 400.
    """


class RetrievalFailedError(Exception):
    """The vector store is unreachable or the hybrid query failed -- `RETRIEVAL_FAILED`, 503.
    Distinct from `ProviderUnavailableError` because the remedies differ entirely.
    """


class ProviderUnavailableError(Exception):
    """No API key, or the provider could not be reached at all -- `AI_UNAVAILABLE`, 503."""


class ProviderTimeoutError(Exception):
    """The provider did not respond inside the call's explicit deadline -- `AI_TIMEOUT`, 504."""


class ProviderRateLimitedError(Exception):
    """The provider rate-limited **us** -- `AI_RATE_LIMITED`, 429. Distinct from this service
    rate-limiting a caller.
    """


class ProviderContentFilteredError(Exception):
    """The provider refused to answer on policy grounds -- `AI_CONTENT_FILTERED`, 422."""
