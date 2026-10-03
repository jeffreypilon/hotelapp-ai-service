"""Retrieval-only orchestration: dense + sparse search, RRF fusion, cross-encoder reranking.

Still no generation -- this module ends with a ranked list of chunks, which is exactly where
AI Step 2 stops. AI Step 3 adds the LangGraph graph that calls into this.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from hotelapp_ai.domain.fusion import reciprocal_rank_fusion
from hotelapp_ai.repositories.chunks import ChunksRepository
from hotelapp_ai.repositories.database import open_connection
from hotelapp_ai.services.ingestion import EmbeddingProvider
from hotelapp_ai.services.reranking import RerankCandidate, Reranker

# pgvector cosine top-k and Postgres full-text top-k, per ai-enablement-overview.md §7.
DENSE_SPARSE_LIMIT = 50

# How many fused candidates are fetched from storage and handed to the reranker. Matches the
# fusion stage's own top-k so nothing beyond what fusion considered relevant reaches reranking.
FUSED_CANDIDATE_LIMIT = 50

# "Rerank, top-50 -> top-5" per ai-enablement-overview.md §7.
RERANK_TOP_N = 5


@dataclass(frozen=True)
class RankedChunk:
    chunk_id: UUID
    heading_path: str | None
    content: str
    fused_score: float
    rerank_score: float


async def retrieve_ranked_chunks(
    *,
    query: str,
    database_url: str,
    embedding_provider: EmbeddingProvider,
    reranker: Reranker,
    chunks_repository: ChunksRepository | None = None,
    top_n: int = RERANK_TOP_N,
) -> list[RankedChunk]:
    """Embeds `query`, runs the hybrid query, fuses the two ranked lists, and reranks the fused
    top candidates down to `top_n`. Returns an already-ranked list -- the caller does no further
    sorting.
    """
    repository = chunks_repository or ChunksRepository()
    embedding = embedding_provider.embed_text(query)

    with open_connection(database_url) as connection:
        hybrid = repository.hybrid_search(
            connection,
            query_embedding=embedding.embedding,
            query_text=query,
            limit=DENSE_SPARSE_LIMIT,
        )
        fused = reciprocal_rank_fusion([hybrid.dense_ids, hybrid.sparse_ids])
        fused_top = fused[:FUSED_CANDIDATE_LIMIT]
        records = repository.get_by_ids(
            connection,
            chunk_ids=[result.chunk_id for result in fused_top],
        )

    if not records:
        return []

    fused_score_by_id = {result.chunk_id: result.score for result in fused_top}
    candidates = [RerankCandidate(chunk_id=record.id, text=record.content) for record in records]

    # The one genuinely CPU-bound step in this path -- never run directly on the event loop.
    reranked = await asyncio.to_thread(
        reranker.rerank,
        query=query,
        candidates=candidates,
        top_n=top_n,
    )

    records_by_id = {record.id: record for record in records}
    return [
        RankedChunk(
            chunk_id=item.chunk_id,
            heading_path=records_by_id[item.chunk_id].heading_path,
            content=records_by_id[item.chunk_id].content,
            fused_score=fused_score_by_id[item.chunk_id],
            rerank_score=item.score,
        )
        for item in reranked
    ]
