"""The LangGraph retrieval graph (F3's guest assistant) and the streaming orchestrator around it.

Corpus-grounded answers only -- no backend calls, no reservation awareness. A guest asking about
their own reservation through the assistant is a future enhancement, not this step; calling
`gateways/hotelapp.py` from this module is a scope violation (phased-implementation-plan.md's
AI Step 3).

The graph's shape mirrors architecture-specification.md's "The retrieval graph" diagram:

    rewrite_query -> retrieve (dense + sparse + fuse + rerank, in one node -- see the note on
    `retrieve_node` below) -> grade -> [sufficient: generate | insufficient: decompose_and_retry
    -> retrieve again, bounded to one retry] -> generate -> end

`generate` is a terminal graph node that only assembles the final context and citations; the
actual token-by-token answer is produced by `AssistantService.ask` *after* the graph resolves,
not inside the graph -- see the module docstring's streaming note below.

> **Judgment call.** The diagram draws `dense_retrieve` and `sparse_retrieve` as two parallel
> edges. `repositories/chunks.py#hybrid_search` already runs both halves in **one** SQL statement
> (Step 2's explicit optimisation, load-bearing for the `EXPLAIN` test), and `services/
> retrieval.retrieve_ranked_chunks` already composes embed -> hybrid search -> fuse -> rerank into
> one call. Re-splitting that into separate graph nodes would mean either issuing the query twice
> or faking a fan-out around a single result -- both worse than one honestly-named `retrieve` node
> that documents what it does. Flagged here rather than silently diffing from the diagram.

> **Streaming, explained.** `POST /assistant/ask` must stream before the whole answer is ready
> (architecture-specification.md's "perceived latency is the first token" decision). Running the
> generation call *inside* `graph.ainvoke` would buffer the whole answer before the graph returns.
> Instead, the graph covers only the deterministic-ish orchestration -- rewrite, retrieve, grade,
> retry -- and resolves to a `generate` node that assembles the prompt and citations without
> calling a model. `AssistantService.ask` then streams the actual generation call once the graph
> is done, which is what makes the first token arrive right after retrieval rather than after the
> full answer.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
from uuid import UUID

import psycopg
import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from hotelapp_ai.domain.assistant_events import (
    AssistantEvent,
    CitationEvent,
    ConversationTurn,
    DoneEvent,
    ErrorEvent,
    TokenEvent,
)
from hotelapp_ai.domain.citations import Citation, render_citations
from hotelapp_ai.domain.errors import (
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QuestionTooLongError,
    RetrievalFailedError,
)
from hotelapp_ai.repositories.database import open_connection
from hotelapp_ai.repositories.documents import DocumentsRepository
from hotelapp_ai.services.ingestion import EmbeddingProvider
from hotelapp_ai.services.llm_client import GenerationDelta, GenerationUsage, LLMClient
from hotelapp_ai.services.reranking import Reranker
from hotelapp_ai.services.retrieval import RankedChunk, retrieve_ranked_chunks

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Bounded to one retry, in code -- the model grades relevance, it never decides how many attempts
# remain. A forced-insufficient grade must terminate after exactly MAX_RETRIES + 1 retrieval
# passes; see tests/unit/test_assistant.py.
MAX_RETRIES = 1

# Per-call ceilings, ai-enablement-overview.md §11: ~500 for generation (a guest-facing answer
# plus citation markers); grading and rewrite are classification-shaped and need far less.
GENERATION_MAX_TOKENS = 500
GRADING_MAX_TOKENS = 150
REWRITE_MAX_TOKENS = 100
MODEL_CALL_TIMEOUT_SECONDS = 10.0


class AssistantState(TypedDict):
    question: str
    history: list[ConversationTurn]
    search_query: str
    context_chunks: list[RankedChunk]
    grade_sufficient: bool
    grade_reason: str
    retry_count: int


@dataclass(frozen=True)
class AssistantDependencies:
    """Everything a graph run needs, bound once at app startup and reused across requests.
    `llm_client` is `None` when no provider is configured -- `AssistantService.ask` checks that
    before touching the graph at all, matching the service's "unconfigured is a valid state"
    posture.
    """

    database_url: str
    embedding_provider: EmbeddingProvider | None
    reranker: Reranker | None
    llm_client: LLMClient | None
    generation_model: str
    grading_model: str
    documents_repository: DocumentsRepository | None = None


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _render_history(history: list[ConversationTurn]) -> str:
    if not history:
        return "(none -- this is the first question)"
    return "\n".join(f"{turn.role}: {turn.content}" for turn in history)


def _render_excerpts(chunks: list[RankedChunk]) -> str:
    if not chunks:
        return "(no excerpts retrieved)"
    parts = []
    for number, chunk in enumerate(chunks, start=1):
        heading = chunk.heading_path or "(no section heading)"
        parts.append(f"{number}. [{heading}]\n{chunk.content}")
    return "\n\n".join(parts)


def _render_documents(chunks: list[RankedChunk], citations: list[Citation]) -> str:
    """Numbers each document with the exact fixed footnote from `domain/citations.py`, so the
    model is asked to cite with the same `[n] Title -- Section` labels the SSE `citation` events
    carry, in the same order.
    """
    footnotes = render_citations(citations)
    return "\n\n".join(
        f"{footnote}\n{chunk.content}" for chunk, footnote in zip(chunks, footnotes, strict=True)
    )


async def _rewrite_query(
    deps: AssistantDependencies,
    *,
    question: str,
    history: list[ConversationTurn],
    retrieval_feedback: str | None,
) -> str:
    if not history and retrieval_feedback is None:
        # No conversational context to resolve and no retry in progress -- a rewrite call would
        # spend a call reproducing its own input. Short-circuits the one case with a knowably
        # identical answer, per coding-standards.md's "every model call is bounded" in spirit:
        # the cheapest bound is not making the call at all.
        return question

    assert deps.llm_client is not None
    prompt = _load_prompt("rewrite_query.md")
    prompt = prompt.replace("<<<HISTORY>>>", _render_history(history))
    prompt = prompt.replace("<<<QUESTION>>>", question)
    prompt = prompt.replace(
        "<<<RETRIEVAL_FEEDBACK>>>",
        retrieval_feedback or "(none -- this is the first retrieval attempt)",
    )
    result = await deps.llm_client.classify_json(
        model=deps.grading_model,
        system_prompt="Follow the instruction section of the supplied prompt exactly.",
        user_prompt=prompt,
        schema_name="rewritten_query",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        max_tokens=REWRITE_MAX_TOKENS,
        timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
    )
    query = str(result["query"]).strip()
    return query or question


_CompiledAssistantGraph = CompiledStateGraph[AssistantState, None, AssistantState, AssistantState]


def _build_graph(deps: AssistantDependencies) -> _CompiledAssistantGraph:
    async def rewrite_query_node(state: AssistantState) -> dict[str, object]:
        query = await _rewrite_query(
            deps,
            question=state["question"],
            history=state["history"],
            retrieval_feedback=None,
        )
        return {"search_query": query}

    async def retrieve_node(state: AssistantState) -> dict[str, object]:
        # `ask()` has already confirmed the provider is configured before the graph runs at all,
        # and `main.py` only wires a non-None embedding provider and reranker in that case --
        # these are never None in a real request. Optional only so a provider-less app can
        # still construct `AssistantDependencies` at startup without needing a placeholder.
        assert deps.embedding_provider is not None
        assert deps.reranker is not None
        try:
            chunks = await retrieve_ranked_chunks(
                query=state["search_query"],
                database_url=deps.database_url,
                embedding_provider=deps.embedding_provider,
                reranker=deps.reranker,
            )
        except psycopg.Error as exc:
            raise RetrievalFailedError(str(exc)) from exc
        return {"context_chunks": chunks}

    async def grade_node(state: AssistantState) -> dict[str, object]:
        chunks = state["context_chunks"]
        if not chunks:
            # No model call needed to grade an empty result as insufficient -- see
            # coding-standards.md's bounded-call rule.
            return {"grade_sufficient": False, "grade_reason": "No chunks were retrieved."}

        assert deps.llm_client is not None
        prompt = _load_prompt("grade_retrieval.md")
        prompt = prompt.replace("<<<QUESTION>>>", state["question"])
        prompt = prompt.replace("<<<EXCERPTS>>>", _render_excerpts(chunks))
        result = await deps.llm_client.classify_json(
            model=deps.grading_model,
            system_prompt="Follow the instruction section of the supplied prompt exactly.",
            user_prompt=prompt,
            schema_name="retrieval_grade",
            schema={
                "type": "object",
                "properties": {
                    "sufficient": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["sufficient", "reason"],
                "additionalProperties": False,
            },
            max_tokens=GRADING_MAX_TOKENS,
            timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
        )
        return {
            "grade_sufficient": bool(result["sufficient"]),
            "grade_reason": str(result["reason"]),
        }

    def route_after_grade(state: AssistantState) -> str:
        if state["grade_sufficient"] or state["retry_count"] >= MAX_RETRIES:
            return "generate"
        return "decompose_and_retry"

    async def decompose_and_retry_node(state: AssistantState) -> dict[str, object]:
        query = await _rewrite_query(
            deps,
            question=state["question"],
            history=state["history"],
            retrieval_feedback=state["grade_reason"],
        )
        return {"search_query": query, "retry_count": state["retry_count"] + 1}

    async def generate_node(state: AssistantState) -> dict[str, object]:
        # Terminal node -- assembles nothing new. It exists so the graph's shape matches the
        # diagram exactly: the conditional edge's "sufficient" branch has somewhere to land that
        # is not END directly, in case a later step needs to do real work here (e.g. caching).
        return {}

    builder: StateGraph[AssistantState] = StateGraph(AssistantState)
    builder.add_node("rewrite_query", rewrite_query_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("grade", grade_node)
    builder.add_node("decompose_and_retry", decompose_and_retry_node)
    builder.add_node("generate", generate_node)

    builder.add_edge(START, "rewrite_query")
    builder.add_edge("rewrite_query", "retrieve")
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges(
        "grade",
        route_after_grade,
        {"generate": "generate", "decompose_and_retry": "decompose_and_retry"},
    )
    builder.add_edge("decompose_and_retry", "retrieve")
    builder.add_edge("generate", END)

    return builder.compile()


def _distinct_document_ids(chunks: list[RankedChunk]) -> list[UUID]:
    seen: list[UUID] = []
    for chunk in chunks:
        if chunk.document_id not in seen:
            seen.append(chunk.document_id)
    return seen


def _build_citations(deps: AssistantDependencies, chunks: list[RankedChunk]) -> list[Citation]:
    if not chunks:
        return []

    documents_repository = deps.documents_repository or DocumentsRepository()
    with open_connection(deps.database_url) as connection:
        records = documents_repository.get_by_ids(
            connection, document_ids=_distinct_document_ids(chunks)
        )
    titles_by_id = {record.id: record.title for record in records}

    return [
        Citation(
            chunk_id=str(chunk.chunk_id),
            document_title=titles_by_id.get(chunk.document_id, "(unknown document)"),
            section=chunk.heading_path,
        )
        for chunk in chunks
    ]


class AssistantService:
    """Owns the compiled graph and streams a corpus-grounded answer for one question.

    One instance per app, built once at startup with `deps` bound -- see `main.py`.
    """

    def __init__(self, deps: AssistantDependencies, *, question_max_length: int) -> None:
        self._deps = deps
        self._question_max_length = question_max_length
        self._graph = _build_graph(deps)

    async def ask(
        self,
        *,
        question: str,
        history: list[ConversationTurn] | None = None,
    ) -> AsyncIterator[AssistantEvent]:
        """Phase 1 (validation, retrieval, grading, retry) runs to completion before this
        generator yields anything, so a phase-1 failure propagates as a raised exception --
        `transport/rest/assistant.py` catches it on the *first* `__anext__()` and turns it into an
        ordinary Problem Details response, per error-handling.md §3's "before the first byte" row.
        Phase 2 (generation) is wrapped in its own `try`/`except` because, by the time it runs,
        the response has already started streaming -- any failure there becomes a terminal
        `error` event instead.
        """
        if len(question) > self._question_max_length:
            raise QuestionTooLongError(f"Question exceeds {self._question_max_length} characters.")
        if self._deps.llm_client is None:
            raise ProviderUnavailableError("No OPENAI_API_KEY is configured.")

        started_at = time.monotonic()
        state: AssistantState = {
            "question": question,
            "history": list(history or []),
            "search_query": question,
            "context_chunks": [],
            "grade_sufficient": False,
            "grade_reason": "",
            "retry_count": 0,
        }
        final_state = await self._graph.ainvoke(state)
        retrieval_ms = (time.monotonic() - started_at) * 1000

        chunks: list[RankedChunk] = final_state["context_chunks"]
        citations = _build_citations(self._deps, chunks)
        for number, citation in enumerate(citations, start=1):
            yield CitationEvent(
                number=number,
                document_title=citation.document_title,
                section=citation.section,
                chunk_id=citation.chunk_id,
            )

        try:
            async for event in self._stream_generation(question, chunks, citations):
                yield event
        except ProviderTimeoutError as exc:
            yield ErrorEvent(code="AI_TIMEOUT", detail=str(exc))
        except ProviderRateLimitedError as exc:
            yield ErrorEvent(code="AI_RATE_LIMITED", detail=str(exc))
        except ProviderContentFilteredError as exc:
            yield ErrorEvent(code="AI_CONTENT_FILTERED", detail=str(exc))
        except ProviderUnavailableError as exc:
            yield ErrorEvent(code="AI_UNAVAILABLE", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 -- last resort so a stream never just stops.
            logger.error("assistant.generation_failed", error=str(exc))
            yield ErrorEvent(code="AI_UNAVAILABLE", detail="The assistant could not finish.")
        finally:
            logger.info(
                "assistant.ask",
                retrievalMs=round(retrieval_ms, 1),
                retryCount=final_state["retry_count"],
                chunkIds=[str(chunk.chunk_id) for chunk in chunks],
            )

    async def _stream_generation(
        self,
        question: str,
        chunks: list[RankedChunk],
        citations: list[Citation],
    ) -> AsyncIterator[AssistantEvent]:
        assert self._deps.llm_client is not None
        prompt = _load_prompt("answer.md")
        prompt = prompt.replace("<<<QUESTION>>>", question)
        prompt = prompt.replace("<<<DOCUMENTS>>>", _render_documents(chunks, citations))

        async for item in self._deps.llm_client.stream_generation(
            model=self._deps.generation_model,
            system_prompt="Follow the instruction section of the supplied prompt exactly.",
            user_prompt=prompt,
            max_tokens=GENERATION_MAX_TOKENS,
            timeout_seconds=MODEL_CALL_TIMEOUT_SECONDS,
        ):
            if isinstance(item, GenerationDelta):
                yield TokenEvent(text=item.text)
            elif isinstance(item, GenerationUsage):
                yield DoneEvent(
                    prompt_tokens=item.prompt_tokens,
                    completion_tokens=item.completion_tokens,
                    cost_usd=str(item.cost_usd),
                )
