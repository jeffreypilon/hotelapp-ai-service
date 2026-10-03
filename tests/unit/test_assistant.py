"""Unit tests for `services/assistant.py`'s LangGraph graph and streaming orchestrator.

No real database, no real provider -- `retrieve_ranked_chunks` and `open_connection` are
monkeypatched, and the LLM client is a hand-written fake, the same pattern
`tests/unit/test_retrieval.py` already uses for `open_connection`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from hotelapp_ai.domain.assistant_events import CitationEvent, DoneEvent, ErrorEvent, TokenEvent
from hotelapp_ai.domain.errors import ProviderTimeoutError, ProviderUnavailableError
from hotelapp_ai.repositories.documents import DocumentRecord
from hotelapp_ai.services.assistant import AssistantDependencies, AssistantService
from hotelapp_ai.services.llm_client import GenerationDelta, GenerationUsage
from hotelapp_ai.services.retrieval import RankedChunk

_DOCUMENT_ID = UUID("0192f3a2-2200-7000-8000-000000000001")
_CHUNK_ID = UUID("0192f3a2-2200-7000-8000-00000000000a")


def _fake_chunk() -> RankedChunk:
    return RankedChunk(
        chunk_id=_CHUNK_ID,
        document_id=_DOCUMENT_ID,
        heading_path="Pets",
        content="Harborview Grand is pet-friendly for dogs, with a $35 per-night fee.",
        fused_score=1.0,
        rerank_score=1.0,
    )


class _FakeDocumentsRepository:
    def get_by_ids(self, connection: object, *, document_ids: list[UUID]) -> list[DocumentRecord]:
        now = datetime.now(UTC)
        return [
            DocumentRecord(
                id=document_id,
                source_path="harborview-grand/10-house-rules.md",
                title="Harborview Grand \u2014 House Rules",
                property_id=None,
                content_hash="hash",
                chunk_count=1,
                ingested_at=now,
                updated_at=now,
            )
            for document_id in document_ids
        ]


@dataclass
class _FakeLLMClient:
    """Records every call so a test can assert on them. `grade_sequence` is consumed in order,
    one entry per `grade` node invocation -- test authors control exactly how many retrieval
    passes a run takes.
    """

    grade_sequence: list[bool] = field(default_factory=list)
    generation_error: Exception | None = None
    grade_calls: int = 0
    rewrite_calls: int = 0
    generation_calls: int = 0

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
        if schema_name == "retrieval_grade":
            sufficient = self.grade_sequence[self.grade_calls]
            self.grade_calls += 1
            return {"sufficient": sufficient, "reason": "test grade"}
        if schema_name == "rewritten_query":
            self.rewrite_calls += 1
            return {"query": "rewritten search query"}
        raise AssertionError(f"unexpected schema_name {schema_name!r}")

    async def stream_generation(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> AsyncIterator[GenerationDelta | GenerationUsage]:
        self.generation_calls += 1
        if self.generation_error is not None:
            raise self.generation_error
        yield GenerationDelta(text="Yes, dogs are welcome.")
        yield GenerationUsage(prompt_tokens=100, completion_tokens=10, cost_usd=Decimal("0.001"))


def _deps(llm_client: _FakeLLMClient | None) -> AssistantDependencies:
    return AssistantDependencies(
        database_url="postgresql://unused",
        embedding_provider=object(),  # type: ignore[arg-type]
        reranker=object(),  # type: ignore[arg-type]
        llm_client=llm_client,  # type: ignore[arg-type]
        generation_model="gpt-5.4-mini",
        grading_model="gpt-5.4-nano",
        documents_repository=_FakeDocumentsRepository(),  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _patch_open_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def _fake_open_connection(database_url: str) -> Any:
        yield object()

    monkeypatch.setattr("hotelapp_ai.services.assistant.open_connection", _fake_open_connection)


def _patch_retrieve(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Returns the list of search queries `retrieve_ranked_chunks` was called with, in order --
    the cheapest way to assert how many retrieval passes a run took.
    """
    queries: list[str] = []

    async def _fake_retrieve(
        *,
        query: str,
        database_url: str,
        embedding_provider: object,
        reranker: object,
        chunks_repository: object | None = None,
        top_n: int = 5,
    ) -> Sequence[RankedChunk]:
        queries.append(query)
        return [_fake_chunk()]

    monkeypatch.setattr("hotelapp_ai.services.assistant.retrieve_ranked_chunks", _fake_retrieve)
    return queries


async def _drain(service: AssistantService, question: str) -> list[object]:
    return [event async for event in service.ask(question=question)]


async def test_sufficient_grade_on_first_pass_retrieves_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _patch_retrieve(monkeypatch)
    llm_client = _FakeLLMClient(grade_sequence=[True])
    service = AssistantService(_deps(llm_client), question_max_length=2000)

    events = await _drain(service, "Can I bring my dog?")

    assert queries == ["Can I bring my dog?"]
    assert llm_client.grade_calls == 1
    assert llm_client.rewrite_calls == 0
    assert llm_client.generation_calls == 1
    assert any(isinstance(event, CitationEvent) for event in events)
    assert any(isinstance(event, TokenEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_forced_insufficient_grade_terminates_after_exactly_two_retrieval_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _patch_retrieve(monkeypatch)
    llm_client = _FakeLLMClient(grade_sequence=[False, False])
    service = AssistantService(_deps(llm_client), question_max_length=2000)

    await _drain(service, "Can I bring my dog?")

    # The first pass uses the original question; the retry uses the model's decomposed/rewritten
    # query -- two passes total, never a third, regardless of the second grade's result.
    assert queries == ["Can I bring my dog?", "rewritten search query"]
    assert llm_client.grade_calls == 2
    assert llm_client.rewrite_calls == 1
    assert llm_client.generation_calls == 1


async def test_question_too_long_raises_before_any_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_retrieve(monkeypatch)
    service = AssistantService(_deps(_FakeLLMClient(grade_sequence=[True])), question_max_length=5)

    with pytest.raises(Exception, match="exceeds 5 characters"):
        await _drain(service, "a much longer question than allowed")


async def test_no_provider_configured_raises_before_any_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_retrieve(monkeypatch)
    service = AssistantService(_deps(None), question_max_length=2000)

    with pytest.raises(ProviderUnavailableError):
        await _drain(service, "Can I bring my dog?")


async def test_generation_failure_becomes_a_terminal_error_event_not_a_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """error-handling.md §3: a failure after streaming has begun is a terminal event, never a
    raised exception reaching the caller -- citations have already been yielded by this point.
    """
    _patch_retrieve(monkeypatch)
    llm_client = _FakeLLMClient(
        grade_sequence=[True], generation_error=ProviderTimeoutError("provider took too long")
    )
    service = AssistantService(_deps(llm_client), question_max_length=2000)

    events = await _drain(service, "Can I bring my dog?")

    assert any(isinstance(event, CitationEvent) for event in events)
    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(error_events) == 1
    assert error_events[0].code == "AI_TIMEOUT"
    assert not any(isinstance(event, DoneEvent) for event in events)
