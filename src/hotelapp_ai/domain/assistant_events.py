"""Pure data shapes crossing the `transport/` <-> `services/` boundary for the guest assistant.

Deliberately `domain/`, not `services/assistant.py` itself: these are plain dataclasses and
exceptions with no I/O, and keeping them here is what lets `transport/rest/assistant.py` import
the event *types* it needs for `isinstance` checks without transitively importing `httpx` or
`psycopg` -- both of which `services/assistant.py` pulls in for the graph's real work, and both of
which module-registry.md's import rules forbid in `transport/`. Importing any name from a module
also executes that module's own imports, so the event shapes cannot live next to the orchestration
logic that uses them without breaking that rule the moment a route needs to recognise one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ConversationTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class TokenEvent:
    text: str


@dataclass(frozen=True)
class CitationEvent:
    number: int
    document_title: str
    section: str | None
    chunk_id: str


@dataclass(frozen=True)
class DoneEvent:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: str
    cache_hit: bool = False


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    detail: str


AssistantEvent = TokenEvent | CitationEvent | DoneEvent | ErrorEvent
