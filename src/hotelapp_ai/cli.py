"""`hotelapp-ai` console entry point."""

from __future__ import annotations

import argparse
import asyncio
import io
import sys

import uvicorn

from hotelapp_ai.config.settings import Settings, get_settings
from hotelapp_ai.domain.assistant_events import CitationEvent, DoneEvent, ErrorEvent, TokenEvent
from hotelapp_ai.repositories.database import assert_ai_schema_ready
from hotelapp_ai.services.assistant import AssistantDependencies, AssistantService
from hotelapp_ai.services.ingestion import OpenAIEmbeddingProvider, run_ingestion
from hotelapp_ai.services.llm_client import LLMClient
from hotelapp_ai.services.reranking import CrossEncoderReranker
from hotelapp_ai.services.retrieval import RankedChunk, retrieve_ranked_chunks


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

    # Found by hand verifying this step on Windows: the streamed answer can legitimately contain
    # a character outside `cp1252` (a non-breaking hyphen, in one real run), and the default
    # console encoding there is not UTF-8. A corpus-grounded answer crashing the one CLI this
    # step's "done means" depends on is worse than a best-effort re-encode.
    if (
        isinstance(sys.stdout, io.TextIOWrapper)
        and sys.stdout.encoding is not None
        and sys.stdout.encoding.lower() != "utf-8"
    ):
        sys.stdout.reconfigure(encoding="utf-8")

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

    args = parser.parse_args()
    if args.command == "serve":
        serve()
    if args.command == "ingest":
        ingest(show_stats=args.stats)
    if args.command == "retrieve":
        retrieve(args.question)
    if args.command == "ask":
        ask(args.question)


if __name__ == "__main__":
    main()
