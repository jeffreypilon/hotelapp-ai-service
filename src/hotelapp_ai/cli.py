"""`hotelapp-ai` console entry point."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys

import httpx
import uvicorn

from hotelapp_ai.config.settings import Settings, get_settings
from hotelapp_ai.domain.assistant_events import CitationEvent, DoneEvent, ErrorEvent, TokenEvent
from hotelapp_ai.domain.errors import (
    BackendProblemError,
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SearchParamsIncompleteError,
)
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.repositories.database import assert_ai_schema_ready
from hotelapp_ai.services.assistant import AssistantDependencies, AssistantService
from hotelapp_ai.services.ingestion import OpenAIEmbeddingProvider, run_ingestion
from hotelapp_ai.services.llm_client import LLMClient
from hotelapp_ai.services.reranking import CrossEncoderReranker
from hotelapp_ai.services.retrieval import RankedChunk, retrieve_ranked_chunks
from hotelapp_ai.services.search import SearchDependencies
from hotelapp_ai.services.search import search as run_search
from hotelapp_ai.transport.mcp.stdio import run as run_mcp_stdio


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "hotelapp_ai.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def ingest(*, show_stats: bool) -> None:
    settings = get_settings()
    print(run_ingestion(settings, show_stats=show_stats))


def retrieve(question: str) -> None:
    """A retrieval-only CLI: ranked chunks for one question, with no generation step. This is
    AI Step 2's "done means" check -- run by hand against a dozen hand-written questions.
    """
    settings = get_settings()
    database_url = settings.require_database_url()
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for `hotelapp-ai retrieve`. Retrieval needs to embed "
            "the question with the same model the corpus was embedded with."
        )
    assert_ai_schema_ready(database_url)

    with OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
    ) as embedding_provider:
        reranker = CrossEncoderReranker()
        ranked = asyncio.run(
            retrieve_ranked_chunks(
                query=question,
                database_url=database_url,
                embedding_provider=embedding_provider,
                reranker=reranker,
            )
        )

    _print_ranked_chunks(ranked)


def _print_ranked_chunks(ranked: list[RankedChunk]) -> None:
    if not ranked:
        print("(no chunks retrieved)")
        return

    for position, chunk in enumerate(ranked, start=1):
        heading = chunk.heading_path or "(no heading)"
        snippet = chunk.content if len(chunk.content) <= 200 else f"{chunk.content[:197]}..."
        print(
            f"{position}. rerank={chunk.rerank_score:.4f} fused={chunk.fused_score:.4f} {heading}"
        )
        print(f"   {snippet}")


def _ensure_utf8_stdout() -> None:
    """Found by hand verifying AI Step 3 on Windows: a model's output can legitimately contain a
    character outside `cp1252` (a non-breaking hyphen, in one real run; an interpunct and an
    en dash, in AI Step 4's), and the default console encoding there is not UTF-8. A correct
    answer crashing the one CLI a step's "done means" depends on is worse than a best-effort
    re-encode.
    """
    if (
        isinstance(sys.stdout, io.TextIOWrapper)
        and sys.stdout.encoding is not None
        and sys.stdout.encoding.lower() != "utf-8"
    ):
        sys.stdout.reconfigure(encoding="utf-8")


def ask(question: str) -> None:
    """Streams a cited, corpus-grounded answer to the terminal. There is no frontend yet --
    item 9 is blocked on both copies of `ui-specifications.md` gaining assistant screens -- so
    this is AI Step 3's "done means" check, mirroring Step 2's `retrieve` exactly.
    """
    settings = get_settings()
    database_url = settings.require_database_url()
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for `hotelapp-ai ask`. The assistant needs it for "
            "embeddings, grading, and generation."
        )
    assert_ai_schema_ready(database_url)
    _ensure_utf8_stdout()

    asyncio.run(_ask_and_print(question, settings=settings, database_url=database_url))


async def _ask_and_print(question: str, *, settings: Settings, database_url: str) -> None:
    assert settings.openai_api_key is not None

    with OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key, model=settings.embedding_model
    ) as embedding_provider:
        llm_client = LLMClient(api_key=settings.openai_api_key)
        try:
            deps = AssistantDependencies(
                database_url=database_url,
                embedding_provider=embedding_provider,
                reranker=CrossEncoderReranker(),
                llm_client=llm_client,
                generation_model=settings.generation_model,
                grading_model=settings.grading_model,
            )
            service = AssistantService(deps, question_max_length=settings.question_max_length)

            citations: list[str] = []
            async for event in service.ask(question=question):
                if isinstance(event, TokenEvent):
                    print(event.text, end="", flush=True)
                elif isinstance(event, CitationEvent):
                    section = f" \u2014 {event.section}" if event.section else ""
                    citations.append(f"[{len(citations) + 1}] {event.document_title}{section}")
                elif isinstance(event, DoneEvent):
                    print()
                    print()
                    print("\n".join(citations) if citations else "(no citations)")
                    print(
                        f"\n(usage: promptTokens={event.prompt_tokens} "
                        f"completionTokens={event.completion_tokens} costUsd={event.cost_usd})"
                    )
                elif isinstance(event, ErrorEvent):
                    print()
                    print(f"[error] {event.code}: {event.detail}")
        finally:
            await llm_client.aclose()


def search(query: str) -> None:
    """Resolves a free-text request into `GET /availability` parameters and runs that search,
    printing the interpretation, the resolved parameters, and the room types found. AI Step 4's
    manual-verification CLI, mirroring `retrieve` and `ask` exactly -- no frontend exists yet.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for `hotelapp-ai search`. Extraction needs it for the "
            "structured-output call."
        )
    _ensure_utf8_stdout()
    asyncio.run(_search_and_print(query, settings=settings))


async def _search_and_print(query: str, *, settings: Settings) -> None:
    assert settings.openai_api_key is not None

    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        gateway = HotelAppGateway(client, settings.hotelapp_api_base_url)
        llm_client = LLMClient(api_key=settings.openai_api_key)
        try:
            deps = SearchDependencies(
                gateway=gateway,
                llm_client=llm_client,
                extraction_model=settings.grading_model,
            )
            try:
                result = await run_search(deps, query=query)
            except SearchParamsIncompleteError as exc:
                print(f"[error] VALIDATION_FAILED: {exc.detail}")
                return
            except BackendProblemError as exc:
                print(f"[error] {exc.code}: {exc.detail}")
                return
            except (
                ProviderTimeoutError,
                ProviderRateLimitedError,
                ProviderContentFilteredError,
                ProviderUnavailableError,
            ) as exc:
                print(f"[error] {type(exc).__name__}: {exc}")
                return
        finally:
            await llm_client.aclose()

    print(f"Interpretation: {result.interpretation}")
    print(f"Parameters: {json.dumps(result.parameters, indent=2)}")

    data = result.results.get("data", [])
    pagination = result.results.get("pagination", {})
    print(f"\n{len(data)} room type(s) found (totalItems={pagination.get('totalItems')}):")
    for row in data:
        room_type = row["roomType"]
        pricing = row["pricing"]
        print(
            f"  - {room_type['name']} ({room_type['code']}): "
            f"${pricing['nightlyRate']}/night, {row['availableRoomCount']} available"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="hotelapp-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the FastAPI app under uvicorn.")
    ingest_parser = subparsers.add_parser("ingest", help="Parse corpus PDFs and store embeddings.")
    ingest_parser.add_argument(
        "--stats",
        action="store_true",
        help="Print persisted chunk/token totals after ingestion.",
    )
    retrieve_parser = subparsers.add_parser(
        "retrieve",
        help="Hybrid retrieval, fusion and reranking for one question. No generation.",
    )
    retrieve_parser.add_argument("question", help="The question to retrieve chunks for.")
    ask_parser = subparsers.add_parser(
        "ask",
        help="Stream a cited, corpus-grounded answer for one question. AI Step 3's manual-"
        "verification CLI -- no frontend exists yet.",
    )
    ask_parser.add_argument("question", help="The question to ask the assistant.")
    search_parser = subparsers.add_parser(
        "search",
        help="Resolve a free-text request into GET /availability parameters and run that "
        "search. AI Step 4's manual-verification CLI -- no frontend exists yet.",
    )
    search_parser.add_argument("query", help="The free-text search request.")
    subparsers.add_parser(
        "mcp-stdio",
        help="Serve MCP over stdio, public tool surface only. AI Step 5 -- what Claude Desktop "
        "launches as a local subprocess.",
    )

    args = parser.parse_args()
    if args.command == "serve":
        serve()
    if args.command == "ingest":
        ingest(show_stats=args.stats)
    if args.command == "retrieve":
        retrieve(args.question)
    if args.command == "ask":
        ask(args.question)
    if args.command == "search":
        search(args.query)
    if args.command == "mcp-stdio":
        run_mcp_stdio()


if __name__ == "__main__":
    main()
