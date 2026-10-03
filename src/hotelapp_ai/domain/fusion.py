"""Reciprocal Rank Fusion -- pure, no I/O.

Combines the dense (pgvector cosine) and sparse (Postgres full-text) ranked lists produced by
`repositories/chunks.py` into one fused ranking, without needing to calibrate two unlike score
scales against each other -- RRF only ever looks at rank position.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

ChunkId = TypeVar("ChunkId", bound=Hashable)

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedResult(Generic[ChunkId]):
    chunk_id: ChunkId
    score: float


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[ChunkId]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[FusedResult[ChunkId]]:
    """Fuses any number of already-ranked (best-first) lists into one ranking.

    A chunk id's fused score is the sum, over every input list it appears in, of
    `1 / (k + rank)`, where `rank` is 1-based. An id present in more than one list -- the case the
    whole fusion step exists for -- accumulates a contribution from each. An id present in only
    one list still scores, just lower on average than one found by both signals.

    Ties are broken deterministically by first-seen order across the input lists (first list
    before second, earlier rank before later), so the same inputs always produce the same output
    order -- there is no hidden dependency on dict iteration order or a stable-sort implementation
    detail.
    """
    scores: dict[ChunkId, float] = {}
    first_seen_order: dict[ChunkId, int] = {}
    next_order = 0

    for ranked_list in ranked_lists:
        for rank, chunk_id in enumerate(ranked_list, start=1):
            if chunk_id not in first_seen_order:
                first_seen_order[chunk_id] = next_order
                next_order += 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    ordered_ids = sorted(
        scores,
        key=lambda chunk_id: (-scores[chunk_id], first_seen_order[chunk_id]),
    )
    return [FusedResult(chunk_id=chunk_id, score=scores[chunk_id]) for chunk_id in ordered_ids]
