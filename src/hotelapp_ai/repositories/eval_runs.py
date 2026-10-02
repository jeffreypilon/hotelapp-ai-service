"""Repository for ai_eval_runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class EvalRunInsert:
    git_sha: str | None
    generation_model: str
    embedding_model: str
    question_count: int
    metrics: dict[str, object]
    passed: bool


@dataclass(frozen=True)
class EvalRunRecord:
    id: UUID
    run_at: datetime
    git_sha: str | None
    generation_model: str
    embedding_model: str
    question_count: int
    metrics: dict[str, object]
    passed: bool


class EvalRunsRepository:
    def insert(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        evaluation: EvalRunInsert,
    ) -> EvalRunRecord:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_eval_runs (
                    git_sha,
                    generation_model,
                    embedding_model,
                    question_count,
                    metrics,
                    passed
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id,
                          run_at,
                          git_sha,
                          generation_model,
                          embedding_model,
                          question_count,
                          metrics,
                          passed
                """,
                (
                    evaluation.git_sha,
                    evaluation.generation_model,
                    evaluation.embedding_model,
                    evaluation.question_count,
                    Jsonb(evaluation.metrics),
                    evaluation.passed,
                ),
            )
            row = cursor.fetchone()

        assert row is not None
        return EvalRunRecord(
            id=cast(UUID, row[0]),
            run_at=cast(datetime, row[1]),
            git_sha=cast(str | None, row[2]),
            generation_model=cast(str, row[3]),
            embedding_model=cast(str, row[4]),
            question_count=cast(int, row[5]),
            metrics=cast(dict[str, object], row[6]),
            passed=cast(bool, row[7]),
        )
