"""Repository for ai_chunks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class ChunkInsert:
    document_id: UUID
    chunk_index: int
    heading_path: str | None
    content: str
    token_count: int
    embedding: list[float]


@dataclass(frozen=True)
class ChunkRecord:
    id: UUID
    document_id: UUID
    chunk_index: int
    heading_path: str | None
    content: str
    token_count: int
    created_at: datetime


class ChunksRepository:
    def delete_by_document_id(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        document_id: UUID,
    ) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_chunks WHERE document_id = %s",
                (document_id,),
            )
            deleted_rows = cursor.rowcount

        return max(deleted_rows, 0)

    def insert_many(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        chunks: list[ChunkInsert],
    ) -> None:
        if not chunks:
            return

        rows = [
            (
                chunk.document_id,
                chunk.chunk_index,
                chunk.heading_path,
                chunk.content,
                chunk.token_count,
                chunk.embedding,
            )
            for chunk in chunks
        ]
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO ai_chunks (
                    document_id,
                    chunk_index,
                    heading_path,
                    content,
                    token_count,
                    embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                rows,
            )

    def list_for_document(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        document_id: UUID,
    ) -> list[ChunkRecord]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,
                       document_id,
                       chunk_index,
                       heading_path,
                       content,
                       token_count,
                       created_at
                FROM ai_chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                (document_id,),
            )
            rows = cursor.fetchall()

        return [
            ChunkRecord(
                id=cast(UUID, row[0]),
                document_id=cast(UUID, row[1]),
                chunk_index=cast(int, row[2]),
                heading_path=cast(str | None, row[3]),
                content=cast(str, row[4]),
                token_count=cast(int, row[5]),
                created_at=cast(datetime, row[6]),
            )
            for row in rows
        ]

    def count_all(self, connection: psycopg.Connection[tuple[object, ...]]) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(id) FROM ai_chunks")
            row = cursor.fetchone()

        assert row is not None
        return cast(int, row[0])

    def sum_tokens(self, connection: psycopg.Connection[tuple[object, ...]]) -> int:
        with connection.cursor() as cursor:
            cursor.execute("SELECT coalesce(sum(token_count), 0) FROM ai_chunks")
            row = cursor.fetchone()

        assert row is not None
        return cast(int, row[0])
