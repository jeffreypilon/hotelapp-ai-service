# hotelapp-ai-service

Python/FastAPI AI service for HotelApp: an MCP server, a retrieval-augmented guest assistant, and
natural-language availability search. Reads business data over the REST contract, never SQL.

## Status

**Step 2 (hybrid retrieval) is now implemented in code.** Still no generation -- this step ends
with a ranked list of chunks, which is exactly where AI Step 2 stops per
`../hotelapp-context/shared/phased-implementation-plan.md`.

Added in this step:
- `repositories/chunks.py`: `hybrid_search`, the dense (pgvector cosine, `ai_chunks_embedding_hnsw_idx`)
  and sparse (Postgres full-text, `ai_chunks_content_tsv_idx`) retrieval in **one** SQL statement,
  each producing its own independently ranked top-50 set; `get_by_ids` returns chunks in caller
  order rather than storage order;
- `domain/fusion.py`: Reciprocal Rank Fusion, pure, unit-tested for ties, one-sided hits, and
  empty inputs;
- `services/reranking.py`: a local `sentence-transformers` cross-encoder
  (`cross-encoder/ms-marco-MiniLM-L-6-v2`), baked into the Docker image at build time so reranking
  never needs network access, run through `asyncio.to_thread` as the one genuinely CPU-bound step
  in the path;
- `services/retrieval.py`: orchestrates embed → hybrid search → fuse → fetch → rerank, returning
  a ranked list of chunks and nothing else;
- `hotelapp-ai retrieve "<question>"`: the retrieval-only CLI this step's "done means" asks for;
- `tests/integration/test_chunks_repository_explain.py`: a real Testcontainers PostgreSQL running
  `pgvector/pgvector:pg18`, migrated from the canonical `shared/migrations-ai/V001__ai_tables.sql`,
  seeded with 40,000 synthetic rows (2,000 was not enough for either index to be chosen -- found by
  hand while writing this test), asserting via `EXPLAIN` that both halves of `HYBRID_SEARCH_SQL`
  use their respective indexes rather than a sequential scan.

**Verified in this environment, end to end, against the real Compose database and a running
Spring Boot backend, with a real `OPENAI_API_KEY`:**
- `uv run hotelapp-ai retrieve "What is the cancellation policy?"` returned the cancellation and
  refund chunks ranked first, from the real 140-chunk corpus ingested in Step 1.
- `uv run hotelapp-ai retrieve "What time is checkout?"` returned the arrival/departure and front
  desk chunks ranked first and second.
- The `EXPLAIN` integration test passes: the dense half uses `ai_chunks_embedding_hnsw_idx`, the
  sparse half uses `ai_chunks_content_tsv_idx`, and neither falls back to a sequential scan.
- `docker build` bakes the cross-encoder's weights in at build time; `docker run --network none`
  against the built image still loads the model and scores a passage, proving the offline
  property holds for reranking the same way Step 0 proved it for the rest of the image.
- All tests (25, including the new `EXPLAIN` suite), `ruff check`/`format --check`, `mypy --strict`,
  and `lint-imports` pass.

### A real finding from this step

Reranking is not uniformly good: a plain-English pet-policy question ("Can I bring my dog?")
ranked an unrelated breakfast-hours chunk above the actual pet-policy chunks, which were still
retrieved and reranked into the top five, just not first. This is plausible behaviour for a small,
general-purpose cross-encoder scoring an out-of-domain corpus it was never tuned on, and tuning
retrieval quality against evidence -- rather than against one hand-run example -- is explicitly
AI Step 3's evaluation-suite territory
(`../hotelapp-context/stacks/ai-service/testing-standards.md#5-the-evaluation-suite`), not this
step's. Recorded here rather than quietly worked around, per the project's standing rule.

**A second finding, caught by CI rather than locally:** `sentence-transformers` pulls in `torch`
as a transitive dependency, and PyPI's default `torch` wheel on Linux bundles a full CUDA stack --
several GB of `nvidia-*` packages this CPU-only reranker never touches. That was enough to exhaust
a GitHub Actions runner's disk building the Docker image (`No space left on device`), despite
building and running correctly on this development machine. Fixed by declaring `torch` as a direct
dependency pinned to PyTorch's official CPU-only index (`tool.uv.sources` /
`tool.uv.index` in `pyproject.toml`) -- the built image dropped from several GB to 2.21 GB, and
`docker run --network none` against it still loads the model and scores a passage offline.

### A note on scope

This repository change was scoped to `hotelapp-ai-service` only, as AI Step 2 requires. No
generation, no LangGraph graph, and no changes to `hotelapp-context` were needed or made.

## Step 1 status (migrations, repositories, and corpus ingestion)

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
uv run hotelapp-ai retrieve "What is the cancellation policy?"   # Step 2: ranked chunks, no generation
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
