"""Pure citation rendering -- chunk plus document metadata in, a fixed footnote string out.

No I/O, no model, no clock. See module-registry.md's `domain/` entry: the format is fixed so the
SSE contract and a future frontend have a stable target --

    [1] Cancellation and Rate-Type Policy -- The standard cancellation window

-- and the number matches the order citations are emitted as `citation` events during the stream.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Citation:
    """One chunk's citation-relevant metadata. `section` is the chunk's `heading_path`, which is
    `None` for a chunk that falls before any sub-heading in its document.
    """

    chunk_id: str
    document_title: str
    section: str | None


def render_citation(number: int, citation: Citation) -> str:
    """Renders the fixed numbered-footnote format. `number` is 1-based and is the caller's
    responsibility -- this function does not track ordering across calls.
    """
    if citation.section:
        return f"[{number}] {citation.document_title} \u2014 {citation.section}"
    return f"[{number}] {citation.document_title}"


def render_citations(citations: list[Citation]) -> list[str]:
    """Renders a whole ranked list, numbered from 1 in the order given -- the order citations are
    emitted as events, per module-registry.md.
    """
    return [render_citation(number, citation) for number, citation in enumerate(citations, start=1)]
