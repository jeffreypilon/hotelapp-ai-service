"""Integration test: the hybrid query chooses the HNSW and GIN indexes, never a sequential scan.

Direct analogue of the Spring Boot test asserting the availability query uses
`reservations_no_overlap_excl` -- an index built for a different operator class is simply never
used, silently, and this is the one test standing between that mistake and a silent latency
regression. Required in AI Step 2, not deferred
(stacks/ai-service/testing-standards.md#3-integration-tests-use-a-real-database-always).

Uses a real Testcontainers PostgreSQL running the actual `pgvector/pgvector:pg18` image, migrated
from the canonical `shared/migrations-ai/V001__ai_tables.sql` -- this service never creates its
own schema, in tests or anywhere.

The seeded embeddings are synthetic random vectors, not provider output. This test asserts a query
*plan*, which Postgres chooses from the column type, the operator, and the row count alone, never
from vector content -- a real `OPENAI_API_KEY` buys this test nothing, so it does not need one and
runs the same with or without one.

**Both halves need a large seed to make the plan choice meaningful** -- Postgres' planner prefers
a sequential scan over either index on a small table regardless of what exists, and this was found
by hand while writing this test: 2,000 rows was not enough for either index to be chosen; 40,000
reliably is. The seed is generated server-side with `generate_series` rather than round-tripping
tens of thousands of 1536-dimension vectors through the Python driver, which was measured at
several minutes for 40,000 rows and is not a cost worth paying on every run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from pgvector.utils import Vector
from testcontainers.community.postgres import PostgresContainer

from hotelapp_ai.repositories.chunks import HYBRID_SEARCH_SQL, ChunksRepository
from hotelapp_ai.repositories.database import open_connection
from hotelapp_ai.repositories.documents import DocumentsRepository

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "hotelapp-context"
    / "shared"
    / "migrations-ai"
    / "V001__ai_tables.sql"
)

_SEED_CHUNK_COUNT = 40_000
_CANCELLATION_CHUNK_COUNT = 20
_EMBEDDING_DIMENSION = 1536
_CANCELLATION_PHRASE = "cancellation policy"

pytestmark = pytest.mark.skipif(
    not _MIGRATION_PATH.exists(),
    reason=f"Canonical AI migration not found at {_MIGRATION_PATH} -- is hotelapp-context "
    "cloned as a sibling of this repository?",
)


@pytest.fixture(scope="module")
def ai_database_url() -> Iterator[str]:
    with PostgresContainer("pgvector/pgvector:pg18") as postgres:
        database_url = postgres.get_connection_url(driver=None)
        _apply_canonical_migration(database_url)
        _seed_chunks(database_url)
        yield database_url


def _apply_canonical_migration(database_url: str) -> None:
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql)


def _seed_chunks(database_url: str) -> None:
    documents_repository = DocumentsRepository()

    with open_connection(database_url) as connection:
        with connection.transaction():
            document = documents_repository.upsert(
                connection,
                source_path="synthetic/explain-seed.md",
                title="EXPLAIN test seed document",
                property_id=None,
                content_hash="0" * 64,
                chunk_count=_SEED_CHUNK_COUNT,
            )

        # Raw, server-side generation -- not `ChunksRepository.insert_many` -- purely for seeding
        # speed. The insert *path* is covered by tests/unit/test_ingestion.py; this fixture only
        # needs rows with the right shape sitting under the real indexes.
        connection.execute(
            """
            INSERT INTO ai_chunks (
                document_id, chunk_index, heading_path, content, token_count, embedding
            )
            SELECT
                %(document_id)s,
                gs,
                'Section ' || gs,
                CASE WHEN gs < %(cancellation_count)s
                     THEN 'Synthetic seeded content, ' || %(phrase)s
                          || ' filler text ' || gs
                     ELSE 'Synthetic seeded content, unrelated amenity and checkout filler '
                          || gs
                END,
                10,
                (
                    SELECT ('[' || string_agg(random()::text, ',') || ']')::vector
                    FROM generate_series(1, %(dimension)s)
                )
            FROM generate_series(0, %(chunk_count)s - 1) AS gs
            """,
            {
                "document_id": document.id,
                "cancellation_count": _CANCELLATION_CHUNK_COUNT,
                "phrase": _CANCELLATION_PHRASE,
                "dimension": _EMBEDDING_DIMENSION,
                "chunk_count": _SEED_CHUNK_COUNT,
            },
        )
        connection.commit()

        # A higher statistics target on the generated tsvector column is what makes the sparse
        # half's selectivity estimate -- and therefore its plan choice -- reliable rather than
        # dependent on ANALYZE's default sample landing on the handful of matching rows.
        connection.execute("ALTER TABLE ai_chunks ALTER COLUMN content_tsv SET STATISTICS 1000")
        connection.execute("ANALYZE ai_chunks")
        connection.commit()


def _explain_hybrid_search(database_url: str, *, query_embedding: list[float]) -> str:
    with open_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"EXPLAIN {HYBRID_SEARCH_SQL}",
            {
                "embedding": Vector(query_embedding),
                "query_text": _CANCELLATION_PHRASE,
                "limit": 50,
            },
        )
        return "\n".join(row[0] for row in cursor.fetchall())


def _fetch_one_seeded_embedding(database_url: str) -> list[float]:
    with open_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT embedding FROM ai_chunks LIMIT 1")
        row = cursor.fetchone()
        assert row is not None
        return list(row[0])


def test_hybrid_search_dense_half_uses_the_hnsw_index(ai_database_url: str) -> None:
    """Runs `EXPLAIN` against the literal SQL text `ChunksRepository.hybrid_search` executes --
    `HYBRID_SEARCH_SQL` is imported, not retyped -- so this assertion cannot drift from what the
    production query actually runs.
    """
    query_embedding = _fetch_one_seeded_embedding(ai_database_url)
    plan_text = _explain_hybrid_search(ai_database_url, query_embedding=query_embedding)

    assert "ai_chunks_embedding_hnsw_idx" in plan_text, plan_text
    assert "Seq Scan on ai_chunks" not in plan_text, plan_text


def test_hybrid_search_sparse_half_uses_the_gin_index(ai_database_url: str) -> None:
    query_embedding = _fetch_one_seeded_embedding(ai_database_url)
    plan_text = _explain_hybrid_search(ai_database_url, query_embedding=query_embedding)

    assert "ai_chunks_content_tsv_idx" in plan_text, plan_text


def test_hybrid_search_returns_independent_ranked_sets(ai_database_url: str) -> None:
    """Calls the production method itself -- not `EXPLAIN` -- to prove the two halves are
    populated and independent, which the plan-only assertions above do not check.
    """
    repository = ChunksRepository()
    query_embedding = _fetch_one_seeded_embedding(ai_database_url)

    with open_connection(ai_database_url) as connection:
        result = repository.hybrid_search(
            connection,
            query_embedding=query_embedding,
            query_text=_CANCELLATION_PHRASE,
            limit=50,
        )

    assert len(result.dense_ids) == 50
    assert len(result.sparse_ids) == _CANCELLATION_CHUNK_COUNT
    assert len(set(result.dense_ids)) == 50
    assert len(set(result.sparse_ids)) == _CANCELLATION_CHUNK_COUNT
