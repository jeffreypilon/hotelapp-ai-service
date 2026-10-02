"""`hotelapp-ai` console entry point."""

from __future__ import annotations

import argparse

import uvicorn

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.services.ingestion import run_ingestion


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

    args = parser.parse_args()
    if args.command == "serve":
        serve()
    if args.command == "ingest":
        ingest(show_stats=args.stats)


if __name__ == "__main__":
    main()
