"""Repository for ai_documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class DocumentRecord:
    id: UUID
    source_path: str
    title: str
    property_id: UUID | None
    content_hash: str
    chunk_count: int
    ingested_at: datetime
    updated_at: datetime


class DocumentsRepository:
    def get_by_source_path(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        source_path: str,
    ) -> DocumentRecord | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,
                       source_path,
                       title,
                       property_id,
                       content_hash,
                       chunk_count,
                       ingested_at,
                       updated_at
                FROM ai_documents
                WHERE source_path = %s
                """,
                (source_path,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return DocumentRecord(
            id=cast(UUID, row[0]),
            source_path=cast(str, row[1]),
            title=cast(str, row[2]),
            property_id=cast(UUID | None, row[3]),
            content_hash=cast(str, row[4]),
            chunk_count=cast(int, row[5]),
            ingested_at=cast(datetime, row[6]),
            updated_at=cast(datetime, row[7]),
        )

    def upsert(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        source_path: str,
        title: str,
        property_id: UUID | None,
        content_hash: str,
        chunk_count: int,
    ) -> DocumentRecord:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_documents (
                    source_path,
                    title,
                    property_id,
                    content_hash,
                    chunk_count
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source_path) DO UPDATE
                SET title = EXCLUDED.title,
                    property_id = EXCLUDED.property_id,
                    content_hash = EXCLUDED.content_hash,
                    chunk_count = EXCLUDED.chunk_count,
                    ingested_at = now(),
                    updated_at = now()
                RETURNING id,
                          source_path,
                          title,
                          property_id,
                          content_hash,
                          chunk_count,
                          ingested_at,
                          updated_at
                """,
                (source_path, title, property_id, content_hash, chunk_count),
            )
            row = cursor.fetchone()

        assert row is not None
        return DocumentRecord(
            id=cast(UUID, row[0]),
            source_path=cast(str, row[1]),
            title=cast(str, row[2]),
            property_id=cast(UUID | None, row[3]),
            content_hash=cast(str, row[4]),
            chunk_count=cast(int, row[5]),
            ingested_at=cast(datetime, row[6]),
            updated_at=cast(datetime, row[7]),
        )

    def count_all(self, connection: psycopg.Connection[tuple[object, ...]]) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(id) FROM ai_documents")
            row = cursor.fetchone()

        assert row is not None
        return cast(int, row[0])
