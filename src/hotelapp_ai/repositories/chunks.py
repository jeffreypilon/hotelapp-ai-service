"""Repository for ai_chunks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import psycopg
from pgvector.utils import Vector  # type: ignore[import-untyped]


@dataclass(frozen=True)
class ChunkInsert:
    document_id: UUID
    chunk_index: int
    heading_path: str | None
    content: str
    token_count: int
    embedding: list[float]


@dataclass(frozen=True)
class ChunkRecord:
    id: UUID
    document_id: UUID
    chunk_index: int
    heading_path: str | None
    content: str
    token_count: int
    created_at: datetime


@dataclass(frozen=True)
class HybridSearchResult:
    """The two ranked id lists `hybrid_search` produces, best-first, each independent of the
    other. Fusing them is `domain/fusion.py`'s job, not this repository's.
    """

    dense_ids: list[UUID]
    sparse_ids: list[UUID]


# Exposed as a module constant, not inlined in `hybrid_search`, so the `EXPLAIN` integration test
# can run `EXPLAIN` against this *exact* SQL text rather than a hand-copied approximation of it --
# the same guarantee the Spring Boot availability-query test gets from calling its real query
# method. See tests/integration/test_chunks_repository_explain.py.
HYBRID_SEARCH_SQL = """
WITH dense AS (
    SELECT id,
           row_number() OVER (ORDER BY embedding <=> %(embedding)s) AS rnk
    FROM ai_chunks
    ORDER BY embedding <=> %(embedding)s
    LIMIT %(limit)s
),
sparse AS (
    SELECT id,
           row_number() OVER (
               ORDER BY ts_rank(
                   content_tsv, websearch_to_tsquery('english', %(query_text)s)
               ) DESC
           ) AS rnk
    FROM ai_chunks
    WHERE content_tsv @@ websearch_to_tsquery('english', %(query_text)s)
    ORDER BY ts_rank(
        content_tsv, websearch_to_tsquery('english', %(query_text)s)
    ) DESC
    LIMIT %(limit)s
)
SELECT 'dense' AS source, id, rnk FROM dense
UNION ALL
SELECT 'sparse' AS source, id, rnk FROM sparse
ORDER BY source, rnk
"""


class ChunksRepository:
    def hybrid_search(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        query_embedding: list[float],
        query_text: str,
        limit: int = 50,
    ) -> HybridSearchResult:
        """Dense (pgvector cosine) and sparse (Postgres full-text) retrieval, in **one**
        statement, each producing its own independently ranked top-`limit` set.

        Each half orders and limits inside its own CTE, with the window function's own `ORDER BY`
        matching the CTE's `ORDER BY` exactly -- that is what lets Postgres choose an index scan
        (`ai_chunks_embedding_hnsw_idx` for the dense half, `ai_chunks_content_tsv_idx` for the
        sparse half) rather than a sequential scan followed by a sort. See the `EXPLAIN` test in
        `tests/integration`, which runs `EXPLAIN` against this exact query text.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                HYBRID_SEARCH_SQL,
                {
                    # `pgvector.psycopg.register_vector` only registers a dumper for
                    # `numpy.ndarray`/`Vector`, not a plain `list` -- that only matters for an
                    # INSERT, where Postgres has an implicit assignment cast from the resulting
                    # `double precision[]` to `vector`. The `<=>` operator below is an expression
                    # context, which has no such implicit cast, so the parameter must be wrapped
                    # explicitly.
                    "embedding": Vector(query_embedding),
                    "query_text": query_text,
                    "limit": limit,
                },
            )
            rows = cursor.fetchall()

        dense_ids: list[UUID] = []
        sparse_ids: list[UUID] = []
        for source, chunk_id, _rank in rows:
            (dense_ids if source == "dense" else sparse_ids).append(cast(UUID, chunk_id))

        return HybridSearchResult(dense_ids=dense_ids, sparse_ids=sparse_ids)

    def get_by_ids(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        chunk_ids: list[UUID],
    ) -> list[ChunkRecord]:
        """Returns the requested chunks in the **same order as `chunk_ids`** -- the caller's
        fused/reranked order -- not the order Postgres happens to return rows in.
        """
        if not chunk_ids:
            return []

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,
                       document_id,
                       chunk_index,
                       heading_path,
                       content,
                       token_count,
                       created_at
                FROM ai_chunks
                WHERE id = ANY(%s)
                """,
                (chunk_ids,),
            )
            rows = cursor.fetchall()

        records_by_id = {
            cast(UUID, row[0]): ChunkRecord(
                id=cast(UUID, row[0]),
                document_id=cast(UUID, row[1]),
                chunk_index=cast(int, row[2]),
                heading_path=cast(str | None, row[3]),
                content=cast(str, row[4]),
                token_count=cast(int, row[5]),
                created_at=cast(datetime, row[6]),
            )
            for row in rows
        }
        return [records_by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in records_by_id]

    def delete_by_document_id(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        document_id: UUID,
    ) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_chunks WHERE document_id = %s",
                (document_id,),
            )
            deleted_rows = cursor.rowcount

        return max(deleted_rows, 0)

    def insert_many(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        chunks: list[ChunkInsert],
    ) -> None:
        if not chunks:
            return

        rows = [
            (
                chunk.document_id,
                chunk.chunk_index,
                chunk.heading_path,
                chunk.content,
                chunk.token_count,
                chunk.embedding,
            )
            for chunk in chunks
        ]
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO ai_chunks (
                    document_id,
                    chunk_index,
                    heading_path,
                    content,
                    token_count,
                    embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                rows,
            )

    def list_for_document(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        document_id: UUID,
    ) -> list[ChunkRecord]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,
                       document_id,
                       chunk_index,
                       heading_path,
                       content,
                       token_count,
                       created_at
                FROM ai_chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                (document_id,),
            )
            rows = cursor.fetchall()

        return [
            ChunkRecord(
                id=cast(UUID, row[0]),
                document_id=cast(UUID, row[1]),
                chunk_index=cast(int, row[2]),
                heading_path=cast(str | None, row[3]),
                content=cast(str, row[4]),
                token_count=cast(int, row[5]),
                created_at=cast(datetime, row[6]),
            )
            for row in rows
        ]

    def count_all(self, connection: psycopg.Connection[tuple[object, ...]]) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(id) FROM ai_chunks")
            row = cursor.fetchone()

        assert row is not None
        return cast(int, row[0])

    def sum_tokens(self, connection: psycopg.Connection[tuple[object, ...]]) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT coalesce(sum(token_count), 0) FROM ai_chunks")
            row = cursor.fetchone()

        assert row is not None
        return cast(int, row[0])
