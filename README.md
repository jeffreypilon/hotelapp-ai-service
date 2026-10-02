# hotelapp-ai-service

Python/FastAPI AI service for HotelApp: an MCP server, a retrieval-augmented guest assistant, and
natural-language availability search. Reads business data over the REST contract, never SQL.

## Status

**Step 1 (migrations, repositories, and corpus ingestion) is now implemented in code.**

Added in this step:
- startup-time schema validation against `pgvector` plus `ai_documents`, `ai_chunks`, and
  `ai_eval_runs`, mirroring Spring Boot's `ddl-auto=validate` posture;
- explicit `psycopg` repositories for all three AI-owned tables;
- deterministic chunking in `domain/chunking.py`;
- `hotelapp-ai ingest` / `hotelapp-ai ingest --stats`, including PDF parsing, normalization,
  heading-path propagation, embeddings, and idempotency by `content_hash`;
- unit tests against the **real rendered PDFs** in `corpus/pdf/`, plus an idempotency test proving
  a second ingest run makes zero embedding calls when the corpus is unchanged.

The canonical setup guide is
`../hotelapp-context/stacks/ai-service/environment-setup-guide.md`. This section records what was
**actually run and verified** for Step 1 in this repository.

## Clone to running, as verified for Step 1

```powershell
# uv is not assumed pre-installed
irm https://astral.sh/uv/install.ps1 | iex
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"   # add to PATH each new terminal session

uv python install 3.13   # if not already present -- uv manages its own interpreter
uv sync --frozen          # NOT `uv add` -- respects the committed uv.lock
cp .env.example .env      # DATABASE_URL now targets the Compose pgvector DB on :5433

docker compose -f ..\hotelapp-context\docker\docker-compose.yml --profile ai up flyway-ai

uv run pytest tests -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run lint-imports
uv run hotelapp-ai serve   # FastAPI on :8000
uv run hotelapp-ai ingest
uv run hotelapp-ai ingest --stats
```

```powershell
curl http://localhost:8000/api/v1/assistant/health
```

**Verified in this environment, end to end, with a real `OPENAI_API_KEY` and a running Spring Boot
backend:**
- `docker compose --profile ai up flyway-ai` created the AI schema against the Compose database on
  `localhost:5433` (run **after** the business migration, so `public` already has a schema history
  table when the AI migration's own baseline step runs — see the ordering note below).
- `assert_ai_schema_ready(...)` passes against that database.
- `uv run hotelapp-ai ingest --stats` against all 16 real corpus documents:
  `documents_seen=16 documents_ingested=16 chunks_embedded=140 embedding_calls=140
  embedding_tokens=9849 embedding_cost_usd=0.0001977`.
- A direct `psql` query against `ai_documents`/`ai_chunks` in the running container confirms 16
  documents and 140 chunks landed, with sane per-document `chunk_count`s.
- Re-running `uv run hotelapp-ai ingest --stats` immediately after: `documents_ingested=0
  documents_skipped=16 chunks_embedded=0 embedding_calls=0` — idempotency by `content_hash` holds
  against the real provider, not just in the unit test.
- Live property-id resolution through `GET /properties` against a running Spring Boot backend
  succeeded as part of the ingest run above.
- All tests, `ruff check`/`format --check`, `mypy`, and `lint-imports` pass with the new
  repositories, ingestion service, and normalization logic. The REST-only boundary grep
  (`reservations|properties|room_types|rooms|users|sessions|payments` over `repositories/`) finds
  nothing.
- The normalization tests use the actual rendered PDFs and assert the three extraction artefacts
  called out in the architecture specification: doubled spaces, running headers/footers, and
  line-wrapped phrases.

**An ordering pitfall worth recording:** on a *freshly created* Compose database, running
`flyway-ai` before the business `flyway` service fails the business migration afterward —
`flyway-ai`'s own baseline leaves `public` non-empty with no `flyway_schema_history` (business)
table yet, and plain Flyway refuses to touch a non-empty schema with no history table of its own.
Apply the business migration (`docker compose --profile spring up`, or any profile that starts
`flyway`) **before** `flyway-ai` against a brand-new volume. This matches the dependency already
documented in `environment-setup-guide.md §2` but is easy to get backwards when standing the AI
service up in isolation.

### A real finding from this step

`shared/acceptance-criteria.md` clearly specifies the **48-hour cancellation window**, but it does
**not** state the 30-night maximum stay. The real rendered-PDF normalization tests therefore pin
the 30-night fact from the corpus plus the API contract, not from acceptance criteria alone.

### A note on scope

This repository change was scoped to `hotelapp-ai-service` only. The already-existing AI migration
in `hotelapp-context/shared/migrations-ai/V001__ai_tables.sql` was **applied**, not edited, and
the canonical setup guide in `hotelapp-context` was left untouched.
