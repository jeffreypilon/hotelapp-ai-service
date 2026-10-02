"""`hotelapp-ai` console entry point. Step 0 implements only `serve`; `ingest`/`eval`/`mcp-stdio`
arrive in later steps, per phased-implementation-plan.md.
"""

from __future__ import annotations

import argparse

import uvicorn

from hotelapp_ai.config.settings import get_settings


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "hotelapp_ai.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="hotelapp-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run the FastAPI app under uvicorn.")

    args = parser.parse_args()
    if args.command == "serve":
        serve()


if __name__ == "__main__":
    main()
