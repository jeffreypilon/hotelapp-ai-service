"""Corpus ingestion: PDF text -> normalized blocks -> chunks -> embeddings -> ai_chunks."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from pypdf import PdfReader

from hotelapp_ai.config.settings import Settings
from hotelapp_ai.domain.chunking import ContentBlock, chunk_blocks
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.repositories.chunks import ChunkInsert, ChunksRepository
from hotelapp_ai.repositories.database import assert_ai_schema_ready, open_connection
from hotelapp_ai.repositories.documents import DocumentRecord, DocumentsRepository

_WHITESPACE_RE = re.compile(r"\s+")
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_REVISION_FOOTER_RE = re.compile(r"^Revision \d{4}\.\d{2} - .+ Page \d+ of \d+$")
_EMBEDDING_PRICES_PER_MILLION_TOKENS = {
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
}


@dataclass(frozen=True)
class MarkdownHeading:
    level: int
    text: str
    path: str


@dataclass(frozen=True)
class CorpusDocument:
    source_path: Path
    pdf_path: Path
    title: str
    property_name: str | None
    headings: tuple[MarkdownHeading, ...]


@dataclass(frozen=True)
class EmbeddingResult:
    embedding: list[float]
    token_count: int
    cost_usd: Decimal


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk_index: int
    heading_path: str | None
    content: str
    token_count: int
    embedding: list[float]


@dataclass(frozen=True)
class PersistedCorpusStats:
    document_count: int
    chunk_count: int
    token_count: int


@dataclass
class IngestionRunStats:
    documents_seen: int = 0
    documents_ingested: int = 0
    documents_skipped: int = 0
    chunks_embedded: int = 0
    embedding_calls: int = 0
    embedding_tokens: int = 0
    embedding_cost_usd: Decimal = Decimal("0")
    persisted: PersistedCorpusStats = PersistedCorpusStats(0, 0, 0)

    def format_summary(self) -> str:
        lines = [
            f"documents_seen={self.documents_seen}",
            f"documents_ingested={self.documents_ingested}",
            f"documents_skipped={self.documents_skipped}",
            f"chunks_embedded={self.chunks_embedded}",
            f"embedding_calls={self.embedding_calls}",
            f"embedding_tokens={self.embedding_tokens}",
            f"embedding_cost_usd={_format_money(self.embedding_cost_usd)}",
        ]
        return "\n".join(lines)

    def format_detailed_summary(self) -> str:
        lines = [
            self.format_summary(),
            f"persisted_documents={self.persisted.document_count}",
            f"persisted_chunks={self.persisted.chunk_count}",
            f"persisted_tokens={self.persisted.token_count}",
        ]
        return "\n".join(lines)


class EmbeddingProvider(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...


class CorpusStore(Protocol):
    def get_document(self, *, source_path: str) -> DocumentRecord | None: ...

    def replace_document(
        self,
        *,
        source_path: str,
        title: str,
        property_id: UUID | None,
        content_hash: str,
        chunks: Sequence[EmbeddedChunk],
    ) -> None: ...

    def summarize(self) -> PersistedCorpusStats: ...


class PostgresCorpusStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._documents = DocumentsRepository()
        self._chunks = ChunksRepository()

    def get_document(self, *, source_path: str) -> DocumentRecord | None:
        with open_connection(self._database_url) as connection:
            return self._documents.get_by_source_path(connection, source_path=source_path)

    def replace_document(
        self,
        *,
        source_path: str,
        title: str,
        property_id: UUID | None,
        content_hash: str,
        chunks: Sequence[EmbeddedChunk],
    ) -> None:
        with open_connection(self._database_url) as connection, connection.transaction():
            document = self._documents.upsert(
                connection,
                source_path=source_path,
                title=title,
                property_id=property_id,
                content_hash=content_hash,
                chunk_count=len(chunks),
            )
            self._chunks.delete_by_document_id(connection, document_id=document.id)
            self._chunks.insert_many(
                connection,
                chunks=[
                    ChunkInsert(
                        document_id=document.id,
                        chunk_index=chunk.chunk_index,
                        heading_path=chunk.heading_path,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        embedding=chunk.embedding,
                    )
                    for chunk in chunks
                ],
            )

    def summarize(self) -> PersistedCorpusStats:
        with open_connection(self._database_url) as connection:
            return PersistedCorpusStats(
                document_count=self._documents.count_all(connection),
                chunk_count=self._chunks.count_all(connection),
                token_count=self._chunks.sum_tokens(connection),
            )


class OpenAIEmbeddingProvider:
    def __init__(self, *, api_key: str, model: str) -> None:
        self._model = model
        self._client = httpx.Client(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(30.0),
        )

    def __enter__(self) -> OpenAIEmbeddingProvider:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def embed_text(self, text: str) -> EmbeddingResult:
        response = self._client.post(
            "/embeddings",
            json={"input": text, "model": self._model},
        )
        response.raise_for_status()
        payload = response.json()
        prompt_tokens = int(payload["usage"]["prompt_tokens"])
        price_per_million = _EMBEDDING_PRICES_PER_MILLION_TOKENS.get(self._model, Decimal("0"))
        cost = (Decimal(prompt_tokens) * price_per_million / Decimal("1000000")).quantize(
            Decimal("0.0000001"),
            rounding=ROUND_HALF_UP,
        )
        return EmbeddingResult(
            embedding=[float(value) for value in payload["data"][0]["embedding"]],
            token_count=prompt_tokens,
            cost_usd=cost,
        )


def run_ingestion(settings: Settings, *, show_stats: bool) -> str:
    database_url = settings.require_database_url()
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for `hotelapp-ai ingest`. The service may start without "
            "one, but ingestion cannot compute embeddings without it."
        )
    assert_ai_schema_ready(database_url)

    corpus_root = _corpus_root()
    corpus_documents = discover_corpus_documents(corpus_root)
    property_ids = _resolve_property_ids(settings, corpus_documents)
    store = PostgresCorpusStore(database_url)

    with OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
    ) as embedding_provider:
        stats = ingest_documents(
            corpus_documents,
            property_ids_by_name=property_ids,
            store=store,
            embedding_provider=embedding_provider,
        )

    return stats.format_detailed_summary() if show_stats else stats.format_summary()


def ingest_documents(
    corpus_documents: Sequence[CorpusDocument],
    *,
    property_ids_by_name: Mapping[str, UUID],
    store: CorpusStore,
    embedding_provider: EmbeddingProvider,
) -> IngestionRunStats:
    stats = IngestionRunStats()
    for document in corpus_documents:
        stats.documents_seen += 1
        source_path = document.source_path.as_posix()
        pdf_bytes = document.pdf_path.read_bytes()
        content_hash = hashlib.sha256(pdf_bytes).hexdigest()
        existing = store.get_document(source_path=source_path)
        if existing is not None and existing.content_hash == content_hash:
            stats.documents_skipped += 1
            continue

        text_pages = extract_pdf_pages(document.pdf_path)
        whitespace_collapsed = collapse_whitespace_runs(text_pages)
        without_furniture = strip_running_headers_and_footers(whitespace_collapsed)
        hard_break_lines = {document.title, *(heading.text for heading in document.headings)}
        blocks = rejoin_wrapped_lines(without_furniture, hard_break_lines=hard_break_lines)
        content_blocks = annotate_heading_paths(
            blocks,
            title=document.title,
            headings=document.headings,
        )
        chunk_candidates = chunk_blocks(content_blocks)

        embedded_chunks: list[EmbeddedChunk] = []
        property_id = _resolve_property_id(document.property_name, property_ids_by_name)
        for chunk in chunk_candidates:
            embedding = embedding_provider.embed_text(chunk.content)
            stats.embedding_calls += 1
            stats.embedding_tokens += embedding.token_count
            stats.embedding_cost_usd += embedding.cost_usd
            embedded_chunks.append(
                EmbeddedChunk(
                    chunk_index=chunk.chunk_index,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    token_count=embedding.token_count,
                    embedding=embedding.embedding,
                )
            )

        store.replace_document(
            source_path=source_path,
            title=document.title,
            property_id=property_id,
            content_hash=content_hash,
            chunks=embedded_chunks,
        )
        stats.documents_ingested += 1
        stats.chunks_embedded += len(embedded_chunks)

    stats.persisted = store.summarize()
    return stats


def discover_corpus_documents(corpus_root: Path) -> list[CorpusDocument]:
    markdown_paths = sorted(
        path
        for path in corpus_root.rglob("*.md")
        if path.parent.name in {"brand", "harborview-grand", "lakeside-inn"}
    )
    documents: list[CorpusDocument] = []
    for markdown_path in markdown_paths:
        title, property_name, headings = parse_markdown_document(markdown_path)
        pdf_path = corpus_root / "pdf" / f"{markdown_path.stem}.pdf"
        if not pdf_path.exists():
            raise RuntimeError(
                f"Rendered PDF missing for {markdown_path.name}. Run "
                "`uv run python scripts/render_corpus.py` first."
            )
        documents.append(
            CorpusDocument(
                source_path=markdown_path.relative_to(corpus_root),
                pdf_path=pdf_path,
                title=title,
                property_name=property_name,
                headings=headings,
            )
        )
    return documents


def parse_markdown_document(
    markdown_path: Path,
) -> tuple[str, str | None, tuple[MarkdownHeading, ...]]:
    lines = markdown_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        raise RuntimeError(f"{markdown_path} is missing required front matter.")

    front_matter: dict[str, str] = {}
    index = 1
    while index < len(lines) and lines[index].strip() != "---":
        key, _, raw_value = lines[index].partition(":")
        front_matter[key.strip()] = raw_value.strip()
        index += 1

    title = front_matter["title"]
    property_name = front_matter.get("property")
    if property_name == "null":
        property_name = None

    headings: list[MarkdownHeading] = []
    heading_stack: list[str] = []
    for line in lines[index + 1 :]:
        match = _MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue

        level = len(match.group(1))
        text = match.group(2).strip()
        heading_stack = heading_stack[: level - 1]
        heading_stack.append(text)
        if level == 1:
            continue
        headings.append(MarkdownHeading(level=level, text=text, path=" > ".join(heading_stack[1:])))

    return title, property_name, tuple(headings)


def extract_pdf_pages(pdf_path: Path) -> list[list[str]]:
    reader = PdfReader(str(pdf_path))
    return [(page.extract_text() or "").splitlines() for page in reader.pages]


def collapse_whitespace_runs(pages: Sequence[Sequence[str]]) -> list[list[str]]:
    return [
        [_WHITESPACE_RE.sub(" ", line).strip() for line in page if line.strip()] for page in pages
    ]


def strip_running_headers_and_footers(pages: Sequence[Sequence[str]]) -> list[list[str]]:
    if not pages:
        return []

    header_candidates = Counter(line for page in pages for line in page[:2])
    repeated_headers = {line for line, count in header_candidates.items() if count >= 2}

    cleaned_pages: list[list[str]] = []
    for page in pages:
        cleaned_pages.append(
            [
                line
                for position, line in enumerate(page)
                if not (position < 2 and line in repeated_headers)
                and not _REVISION_FOOTER_RE.match(line)
            ]
        )
    return cleaned_pages


def rejoin_wrapped_lines(
    pages: Sequence[Sequence[str]],
    *,
    hard_break_lines: Iterable[str],
) -> list[str]:
    hard_breaks = set(hard_break_lines)
    blocks: list[str] = []
    current = ""
    for page in pages:
        for line in page:
            if line in hard_breaks:
                if current:
                    blocks.append(current)
                    current = ""
                blocks.append(line)
                continue

            if not current:
                current = line
                continue

            if _should_join_lines(current, line):
                current = f"{current} {line}"
            else:
                blocks.append(current)
                current = line

        if current:
            blocks.append(current)
            current = ""

    return blocks


def annotate_heading_paths(
    blocks: Sequence[str],
    *,
    title: str,
    headings: Sequence[MarkdownHeading],
) -> list[ContentBlock]:
    content_blocks: list[ContentBlock] = []
    heading_index = 0
    current_heading_path: str | None = None

    for block in blocks:
        if block == title:
            continue

        if heading_index < len(headings) and block == headings[heading_index].text:
            current_heading_path = headings[heading_index].path
            heading_index += 1
            continue

        content_blocks.append(ContentBlock(text=block, heading_path=current_heading_path))

    return content_blocks


def _resolve_property_id(
    property_name: str | None,
    property_ids_by_name: Mapping[str, UUID],
) -> UUID | None:
    if property_name is None:
        return None
    try:
        return property_ids_by_name[property_name]
    except KeyError as exc:
        raise RuntimeError(
            f"Property {property_name!r} is present in corpus front matter but was not returned by "
            "GET /properties."
        ) from exc


def _resolve_property_ids(
    settings: Settings,
    documents: Sequence[CorpusDocument],
) -> dict[str, UUID]:
    property_names = sorted(
        {document.property_name for document in documents if document.property_name}
    )
    if not property_names:
        return {}

    async def _fetch_property_ids() -> dict[str, UUID]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            gateway = HotelAppGateway(client, settings.hotelapp_api_base_url)
            properties = await gateway.list_properties()
            return {
                str(item["name"]): UUID(str(item["id"]))
                for item in properties
                if item["name"] in property_names
            }

    try:
        property_ids = asyncio.run(_fetch_property_ids())
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "Property-specific corpus documents require the HotelApp backend to resolve property "
            f"IDs via GET /properties, but {settings.hotelapp_api_base_url}/properties was "
            f"unreachable: {exc}"
        ) from exc

    missing_names = [name for name in property_names if name not in property_ids]
    if missing_names:
        raise RuntimeError(
            "GET /properties did not return every property named in corpus front matter: "
            + ", ".join(missing_names)
        )
    return property_ids


def _should_join_lines(current: str, next_line: str) -> bool:
    return not current.endswith((".", "!", "?", ":", ";"))


def _corpus_root() -> Path:
    return Path(__file__).resolve().parents[3] / "corpus"


def _format_money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP), "f")
