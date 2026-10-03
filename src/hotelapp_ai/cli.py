"""`hotelapp-ai` console entry point."""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.repositories.database import assert_ai_schema_ready
from hotelapp_ai.services.ingestion import OpenAIEmbeddingProvider, run_ingestion
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

    args = parser.parse_args()
    if args.command == "serve":
        serve()
    if args.command == "ingest":
        ingest(show_stats=args.stats)
    if args.command == "retrieve":
        retrieve(args.question)


if __name__ == "__main__":
    main()
