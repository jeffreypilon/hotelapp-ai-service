"""Pure, deterministic chunk-boundary selection with heading propagation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ContentBlock:
    text: str
    heading_path: str | None


@dataclass(frozen=True)
class ChunkCandidate:
    chunk_index: int
    heading_path: str | None
    content: str


def chunk_blocks(
    blocks: Sequence[ContentBlock],
    *,
    max_chars: int = 1200,
) -> list[ChunkCandidate]:
    chunks: list[ChunkCandidate] = []
    current_heading: str | None = None
    current_parts: list[str] = []
    current_length = 0

    for block in blocks:
        for segment in _split_block(block.text, max_chars=max_chars):
            if current_parts and (
                block.heading_path != current_heading
                or current_length + 1 + len(segment) > max_chars
            ):
                chunks.append(
                    ChunkCandidate(
                        chunk_index=len(chunks),
                        heading_path=current_heading,
                        content=" ".join(current_parts),
                    )
                )
                current_parts = []
                current_length = 0

            if not current_parts:
                current_heading = block.heading_path
                current_parts.append(segment)
                current_length = len(segment)
                continue

            current_parts.append(segment)
            current_length += 1 + len(segment)

    if current_parts:
        chunks.append(
            ChunkCandidate(
                chunk_index=len(chunks),
                heading_path=current_heading,
                content=" ".join(current_parts),
            )
        )

    return chunks


def _split_block(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    sentences = [
        sentence.strip() for sentence in _SENTENCE_BOUNDARY_RE.split(text) if sentence.strip()
    ]
    if len(sentences) == 1:
        return _split_long_sentence(text, max_chars=max_chars)

    segments: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
            continue

        candidate = f"{current} {sentence}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        segments.append(current)
        current = sentence

    if current:
        segments.append(current)

    flattened: list[str] = []
    for segment in segments:
        if len(segment) <= max_chars:
            flattened.append(segment)
        else:
            flattened.extend(_split_long_sentence(segment, max_chars=max_chars))
    return flattened


def _split_long_sentence(text: str, *, max_chars: int) -> list[str]:
    words = text.split()
    segments: list[str] = []
    current_words: list[str] = []

    for word in words:
        candidate = " ".join([*current_words, word])
        if current_words and len(candidate) > max_chars:
            segments.append(" ".join(current_words))
            current_words = [word]
            continue

        current_words.append(word)

    if current_words:
        segments.append(" ".join(current_words))

    return segments
