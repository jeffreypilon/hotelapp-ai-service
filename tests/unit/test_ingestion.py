from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from hotelapp_ai.repositories.documents import DocumentRecord
from hotelapp_ai.services.ingestion import (
    EmbeddedChunk,
    EmbeddingResult,
    MarkdownHeading,
    PersistedCorpusStats,
    annotate_heading_paths,
    collapse_whitespace_runs,
    discover_corpus_documents,
    extract_pdf_pages,
    ingest_documents,
    rejoin_wrapped_lines,
    strip_running_headers_and_footers,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_ROOT = _REPO_ROOT / "corpus"


@dataclass
class _StoredDocument:
    record: DocumentRecord
    chunks: list[EmbeddedChunk]


class _InMemoryCorpusStore:
    def __init__(self) -> None:
        self._documents: dict[str, _StoredDocument] = {}

    def get_document(self, *, source_path: str) -> DocumentRecord | None:
        stored = self._documents.get(source_path)
        return None if stored is None else stored.record

    def replace_document(
        self,
        *,
        source_path: str,
        title: str,
        property_id: UUID | None,
        content_hash: str,
        chunks: Sequence[EmbeddedChunk],
    ) -> None:
        existing = self._documents.get(source_path)
        document_id = None if existing is None else existing.record.id
        record = _document_record(
            source_path,
            title,
            property_id,
            content_hash,
            len(chunks),
            document_id=document_id,
        )
        self._documents[source_path] = _StoredDocument(record=record, chunks=list(chunks))

    def summarize(self) -> PersistedCorpusStats:
        chunk_count = sum(len(stored.chunks) for stored in self._documents.values())
        token_count = sum(
            chunk.token_count for stored in self._documents.values() for chunk in stored.chunks
        )
        return PersistedCorpusStats(
            document_count=len(self._documents),
            chunk_count=chunk_count,
            token_count=token_count,
        )


class _CountingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed_text(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            embedding=[0.1, 0.2, 0.3],
            token_count=len(text.split()),
            cost_usd=Decimal("0.0000001"),
        )


def test_collapse_whitespace_runs_uses_real_rendered_pdf() -> None:
    pages = extract_pdf_pages(_CORPUS_ROOT / "pdf" / "01-cancellation-and-rate-policy.pdf")

    expected_raw = "A  single  reservation  may  cover  a  maximum  of  30  nights."
    assert any(expected_raw in line for line in pages[1])

    collapsed = collapse_whitespace_runs(pages)

    expected_normalized = "A single reservation may cover a maximum of 30 nights."
    assert any(expected_normalized in line for line in collapsed[1])
    assert not any("  " in line for page in collapsed for line in page)


def test_strip_running_headers_and_footers_uses_real_rendered_pdf() -> None:
    pages = collapse_whitespace_runs(
        extract_pdf_pages(_CORPUS_ROOT / "pdf" / "06-meetings-and-events.pdf")
    )

    stripped = strip_running_headers_and_footers(pages)
    stripped_lines = [line for page in stripped for line in page]

    assert "HotelApp Hotels | Meetings and Events" not in stripped_lines
    assert not any(
        line.startswith("Revision 2026.10 - Guest Services Page") for line in stripped_lines
    )


def test_rejoin_wrapped_lines_uses_real_rendered_pdf() -> None:
    documents = discover_corpus_documents(_CORPUS_ROOT)
    document = next(
        item for item in documents if item.pdf_path.name == "01-cancellation-and-rate-policy.pdf"
    )
    pages = collapse_whitespace_runs(extract_pdf_pages(document.pdf_path))
    stripped = strip_running_headers_and_footers(pages)

    blocks = rejoin_wrapped_lines(
        stripped,
        hard_break_lines={document.title, *(heading.text for heading in document.headings)},
    )

    expected_block = (
        "A single reservation may cover a maximum of 30 nights. Longer stays are arranged as "
        "consecutive reservations or through a group agreement; the front desk can set either up."
    )
    assert expected_block in blocks


def test_annotate_heading_paths_follows_markdown_headings() -> None:
    headings = (
        MarkdownHeading(
            level=2,
            text="The standard cancellation window",
            path="The standard cancellation window",
        ),
        MarkdownHeading(level=2, text="Length of stay", path="Length of stay"),
    )

    blocks = [
        "Cancellation and Rate-Type Policy",
        "The standard cancellation window",
        (
            "Reservations on our Flexible rate may be cancelled up to 48 hours before check-in "
            "with a full refund."
        ),
        "Length of stay",
        "A single reservation may cover a maximum of 30 nights.",
    ]

    content_blocks = annotate_heading_paths(
        blocks,
        title="Cancellation and Rate-Type Policy",
        headings=headings,
    )

    assert content_blocks[0].heading_path == "The standard cancellation window"
    assert content_blocks[1].heading_path == "Length of stay"


def test_ingest_skips_unchanged_documents_without_embedding_calls() -> None:
    store = _InMemoryCorpusStore()
    embedding_provider = _CountingEmbeddingProvider()
    documents = discover_corpus_documents(_CORPUS_ROOT)
    property_ids = {
        "Harborview Grand": UUID("0192f3a2-1100-7000-8000-000000000001"),
        "Lakeside Inn": UUID("0192f3a2-1100-7000-8000-000000000002"),
    }

    first_run = ingest_documents(
        documents,
        property_ids_by_name=property_ids,
        store=store,
        embedding_provider=embedding_provider,
    )

    first_call_count = embedding_provider.calls

    second_run = ingest_documents(
        documents,
        property_ids_by_name=property_ids,
        store=store,
        embedding_provider=embedding_provider,
    )

    assert first_run.documents_ingested == 16
    assert first_call_count > 0
    assert second_run.documents_ingested == 0
    assert second_run.documents_skipped == 16
    assert second_run.embedding_calls == 0
    assert embedding_provider.calls == first_call_count


def _document_record(
    source_path: str,
    title: str,
    property_id: UUID | None,
    content_hash: str,
    chunk_count: int,
    *,
    document_id: UUID | None = None,
) -> DocumentRecord:
    return DocumentRecord(
        id=document_id or uuid4(),
        source_path=source_path,
        title=title,
        property_id=property_id,
        content_hash=content_hash,
        chunk_count=chunk_count,
        ingested_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
