"""Shared database helpers for the AI-owned schema only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]

REQUIRED_AI_TABLES = ("ai_documents", "ai_chunks", "ai_eval_runs")


@contextmanager
def open_connection(database_url: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(database_url) as connection:
        register_vector(connection)
        yield connection


def assert_ai_schema_ready(database_url: str) -> None:
    with open_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT extname FROM pg_extension WHERE extname = %s",
            ("vector",),
        )
        has_vector_extension = cursor.fetchone() is not None

        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name = ANY(%s)
            ORDER BY table_name
            """,
            ("public", list(REQUIRED_AI_TABLES)),
        )
        existing_tables = {row[0] for row in cursor.fetchall()}

    if not has_vector_extension:
        raise RuntimeError(
            "DATABASE_URL points to a database without the pgvector `vector` extension. "
            "Apply hotelapp-context/shared/migrations-ai/V001__ai_tables.sql against the "
            "Compose database on localhost:5433, then restart the service."
        )

    missing_tables = [
        table_name for table_name in REQUIRED_AI_TABLES if table_name not in existing_tables
    ]
    if missing_tables:
        missing_list = ", ".join(missing_tables)
        raise RuntimeError(
            "DATABASE_URL points to a database missing required AI tables: "
            f"{missing_list}. Apply hotelapp-context/shared/migrations-ai/"
            "V001__ai_tables.sql, then restart the service."
        )
