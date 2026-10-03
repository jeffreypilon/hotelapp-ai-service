from __future__ import annotations

from hotelapp_ai.domain.citations import Citation, render_citation, render_citations


def test_render_citation_with_section() -> None:
    citation = Citation(
        chunk_id="c1",
        document_title="Cancellation and Rate-Type Policy",
        section="The standard cancellation window",
    )

    rendered = render_citation(1, citation)

    assert rendered == (
        "[1] Cancellation and Rate-Type Policy \u2014 The standard cancellation window"
    )


def test_render_citation_without_section_omits_the_dash() -> None:
    citation = Citation(chunk_id="c2", document_title="House Rules", section=None)

    rendered = render_citation(3, citation)

    assert rendered == "[3] House Rules"


def test_render_citations_numbers_in_order_given() -> None:
    citations = [
        Citation(chunk_id="a", document_title="Policy A", section="Section A"),
        Citation(chunk_id="b", document_title="Policy B", section=None),
        Citation(chunk_id="c", document_title="Policy C", section="Section C"),
    ]

    rendered = render_citations(citations)

    assert rendered == [
        "[1] Policy A \u2014 Section A",
        "[2] Policy B",
        "[3] Policy C \u2014 Section C",
    ]
