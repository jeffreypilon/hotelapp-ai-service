"""Local cross-encoder reranking.

The single largest precision gain in the retrieval pipeline (ai-enablement-overview.md §7), and
the one genuinely CPU-bound step in the request path -- callers run `rerank` through
`asyncio.to_thread`, never directly on the event loop (coding-standards.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

# Baked into the Docker image at build time (see Dockerfile) so scoring a passage never triggers
# a first-use download, which would break the offline property Phase 8 established.
RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: UUID
    text: str


@dataclass(frozen=True)
class RerankedChunk:
    chunk_id: UUID
    score: float


class Reranker(Protocol):
    def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[RerankCandidate],
        top_n: int,
    ) -> list[RerankedChunk]: ...


class CrossEncoderReranker:
    """Wraps a local `sentence_transformers.CrossEncoder`.

    The import is deferred to `__init__` rather than module level: it pulls in `torch`, which is
    heavy and irrelevant to every module that does not actually rerank (unit tests included -- see
    `services/retrieval.py`'s tests, which inject a fake `Reranker` instead of this one).
    """

    def __init__(self, *, model_name: str = RERANKER_MODEL_NAME) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model_name)

    def rerank(
        self,
        *,
        query: str,
        candidates: Sequence[RerankCandidate],
        top_n: int,
    ) -> list[RerankedChunk]:
        if not candidates:
            return []

        pairs = [(query, candidate.text) for candidate in candidates]
        raw_scores = self._model.predict(pairs)

        scored = [
            RerankedChunk(chunk_id=candidate.chunk_id, score=float(raw_score))
            for candidate, raw_score in zip(candidates, raw_scores, strict=True)
        ]
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_n]
