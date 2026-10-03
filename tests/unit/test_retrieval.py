from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from hotelapp_ai.repositories.chunks import ChunkRecord, ChunksRepository, HybridSearchResult
from hotelapp_ai.services.ingestion import EmbeddingResult
from hotelapp_ai.services.reranking import RerankCandidate, RerankedChunk
from hotelapp_ai.services.retrieval import retrieve_ranked_chunks

_CHUNK_A = UUID("0192f3a2-1100-7000-8000-00000000000a")
_CHUNK_B = UUID("0192f3a2-1100-7000-8000-00000000000b")
_CHUNK_C = UUID("0192f3a2-1100-7000-8000-00000000000c")
_DOCUMENT_ID = UUID("0192f3a2-1100-7000-8000-000000000001")


class _FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed_text(self, text: str) -> EmbeddingResult:
        self.embedded_texts.append(text)
        return EmbeddingResult(embedding=[0.1, 0.2, 0.3], token_count=3, cost_usd=Decimal("0"))


class _FakeReranker:
    """Reverses the handed-in candidate order and scores by position, so a test can assert the
    retrieval orchestration actually passed the reranker's output through rather than the fused
    order.
    """

    def __init__(self) -> None:
        self.received_queries: list[str] = []
        self.received_top_n: list[int] = []

    def rerank(
        self,
        *,
        query: str,
        candidates: list[RerankCandidate],
        top_n: int,
    ) -> list[RerankedChunk]:
        self.received_queries.append(query)
        self.received_top_n.append(top_n)
        reversed_candidates = list(reversed(candidates))
        return [
            RerankedChunk(chunk_id=candidate.chunk_id, score=float(len(reversed_candidates) - i))
            for i, candidate in enumerate(reversed_candidates)
        ][:top_n]


class _FakeChunksRepository(ChunksRepository):
    def __init__(self, *, hybrid_result: HybridSearchResult, records: list[ChunkRecord]) -> None:
        self._hybrid_result = hybrid_result
        self._records_by_id = {record.id: record for record in records}

    def hybrid_search(  # type: ignore[override]
        self,
        connection: object,
        *,
        query_embedding: list[float],
        query_text: str,
        limit: int = 50,
    ) -> HybridSearchResult:
        return self._hybrid_result

    def get_by_ids(  # type: ignore[override]
        self,
        connection: object,
        *,
        chunk_ids: list[UUID],
    ) -> list[ChunkRecord]:
        return [
            self._records_by_id[chunk_id]
            for chunk_id in chunk_ids
            if chunk_id in self._records_by_id
        ]


def _chunk_record(chunk_id: UUID, *, heading_path: str | None, content: str) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        document_id=_DOCUMENT_ID,
        chunk_index=0,
        heading_path=heading_path,
        content=content,
        token_count=10,
        created_at=datetime.now(UTC),
    )


class _NullConnection:
    """Stands in for a psycopg connection: the fake repository never touches it, and
    `open_connection` is monkeypatched below so no real database is involved.
    """


@pytest.fixture(autouse=True)
def _patch_open_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    @contextlib.contextmanager
    def _fake_open_connection(database_url: str):  # type: ignore[no-untyped-def]
        yield _NullConnection()

    monkeypatch.setattr(
        "hotelapp_ai.services.retrieval.open_connection",
        _fake_open_connection,
    )


async def test_retrieve_ranked_chunks_fuses_and_reranks() -> None:
    hybrid_result = HybridSearchResult(
        dense_ids=[_CHUNK_A, _CHUNK_B],
        sparse_ids=[_CHUNK_B, _CHUNK_C],
    )
    records = [
        _chunk_record(_CHUNK_A, heading_path="Policy > A", content="Content A"),
        _chunk_record(_CHUNK_B, heading_path="Policy > B", content="Content B"),
        _chunk_record(_CHUNK_C, heading_path="Policy > C", content="Content C"),
    ]
    repository = _FakeChunksRepository(hybrid_result=hybrid_result, records=records)
    embedding_provider = _FakeEmbeddingProvider()
    reranker = _FakeReranker()

    ranked = await retrieve_ranked_chunks(
        query="what is the cancellation window?",
        database_url="postgresql://unused",
        embedding_provider=embedding_provider,
        reranker=reranker,
        chunks_repository=repository,
        top_n=2,
    )

    assert embedding_provider.embedded_texts == ["what is the cancellation window?"]
    assert reranker.received_queries == ["what is the cancellation window?"]
    assert reranker.received_top_n == [2]
    assert len(ranked) == 2
    assert all(chunk.rerank_score > 0 for chunk in ranked)
    assert ranked[0].rerank_score >= ranked[1].rerank_score


async def test_retrieve_ranked_chunks_returns_empty_when_nothing_found() -> None:
    hybrid_result = HybridSearchResult(dense_ids=[], sparse_ids=[])
    repository = _FakeChunksRepository(hybrid_result=hybrid_result, records=[])
    embedding_provider = _FakeEmbeddingProvider()
    reranker = _FakeReranker()

    ranked = await retrieve_ranked_chunks(
        query="an unanswerable question",
        database_url="postgresql://unused",
        embedding_provider=embedding_provider,
        reranker=reranker,
        chunks_repository=repository,
    )

    assert ranked == []
    assert reranker.received_queries == []
