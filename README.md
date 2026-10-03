# hotelapp-ai-service

Python/FastAPI AI service for HotelApp: an MCP server, a retrieval-augmented guest assistant, and
natural-language availability search. Reads business data over the REST contract, never SQL.

## Status

**Step 4 (natural-language availability search, F2) is now implemented in code.**
`POST /assistant/search` resolves a free-text request into `GET /availability`'s own parameter
set via one structured-output call, then runs that search -- named/city-implied property, one
call; no property resolvable, one call per property with the results merged, re-sorted, and
re-paginated. No LangGraph here: a single structured-output call does not need a graph, per
`../hotelapp-context/shared/phased-implementation-plan.md`'s AI Step 4.

Added in this step:
- `domain/errors.py` gains `SearchParamsIncompleteError` (extraction itself noticed a missing
  check-in/check-out date -- `400 VALIDATION_FAILED` with a plain-language `detail`, never a
  guessed date) and `BackendProblemError` (a `GET /availability` Problem Details response,
  carried through unmodified -- error-handling.md §2's "translated, never re-interpreted" rule);
- `gateways/hotelapp.py` gains `get_availability()` -- the raw response, same style as
  `get_properties()`; interpreting the body, including Problem Details passthrough, stays the
  caller's job;
- `services/search.py`: the one structured-output call (schema-constrained `roomTypeCode`,
  `rateCategory`, `amenityCode` enums sourced from `data-model.md`/`V001__initial_schema.sql`;
  `propertyId` constrained to the **live** `get_properties()` list every call, never a name baked
  into the prompt), the named/unnamed-property branch, and `_merge_availability`'s re-sort/
  re-paginate over the combined set;
- `transport/rest/search.py`: `POST /assistant/search`'s request/response shapes and the Problem
  Details mapping for every exception `services/search.py` can raise -- untyped
  `request.app.state.search_service`, same pattern `rest/assistant.py` uses, so this module never
  transitively imports `httpx`/`psycopg` via `services/`;
- `prompts/extract_search_params.md`;
- `hotelapp-ai search "<query>"`: this step's manual-verification CLI, mirroring `ask`/`retrieve`
  exactly -- no frontend exists yet;
- `eval/search_smoke_questions.yaml`: the six cases phased-implementation-plan.md's AI Step 4
  instructions name explicitly -- a named property, an unnamed property (fan-out), a relative
  date, an amenity request, a rate-category phrase, and the missing-dates case.

### Three real findings from this step, found by hand with the CLI

All three are prompt gaps, not code bugs -- the structured-output schema made every value
*structurally* valid, but the **nano** tier still needed more explicit instruction to derive date
arithmetic reliably, and once, consistently, run to run:

1. **"Next weekend" resolved to `null` dates on the first run.** The model was given today's date
   as a bare ISO string and did not reliably derive its own day of the week from it. Fixed by
   rendering `<<<TODAY>>>` as `"2026-10-03 (Saturday)"` -- spelling out the weekday removes that
   arithmetic from the model entirely.
2. **A stated check-in date plus "for one night" also resolved to `null` `checkOutDate`.** The
   model did not treat a stay length as a second date, even with an explicit check-in date given.
   Fixed by adding an explicit rule: a stated check-in plus a stated stay length is a *found* date
   (check-in plus that many days), not a guessed one -- only the complete absence of a second date
   or a stay length should produce `null`.
3. **"Next weekend" resolved to a Sunday checkout in some runs and a Monday checkout in
   others, on the identical query** -- a second finding that survived the first fix, because a
   single verification run cannot see a probabilistic failure. "The Friday through Sunday
   following today" is genuinely ambiguous in English (does the stay include Sunday night or
   not?), and the model picked a different reading on different calls to the same input. Fixed by
   replacing the day-range phrase with an explicit table stating the night count and the exact
   checkout day ("a two-night stay that does not include Sunday night") instead of restating the
   same ambiguous phrase more firmly. **Verified by sampling the identical query 8 times for the
   raw extraction and 5 more times through the full CLI** -- all 13 runs produced the identical
   `checkInDate`/`checkOutDate` pair. While fixing this, the same table also pins down "this
   weekend" the same way; "a long weekend" is explicitly left as an unresolvable phrase (`null`
   dates) rather than guessing which extra night it means, since no two readers agree on that
   either and it is not required by the current smoke set.

None of these would have been visible without actually reading the interpretation and parameters
by hand -- and the third was only visible by reading *several* runs, not one -- which is exactly
what this step's "done means" asks for rather than scoring.

**Verified in this environment, end to end, against the real Compose database, a running Spring
Boot backend, and a real `OPENAI_API_KEY`:**
- All six `eval/search_smoke_questions.yaml` cases, run with `uv run hotelapp-ai search "<query>"`:
  - A named property ("...at Harborview Grand...") resolved to Harborview Grand's real id and
    made exactly one `GET /availability` call, returning its four Harbor View room types.
  - An unnamed property ("A room for two guests...") resolved `propertyId: null`, made one call
    per property, and the merged result genuinely contained both Lakeside Inn and Harbor View
    room types, re-sorted by nightly rate ascending across the combined set.
  - "A room for two, next weekend" (today was Saturday 2026-10-03) resolved to
    `checkInDate: "2026-10-09"` / `checkOutDate: "2026-10-11"` -- the following Friday to Sunday,
    a two-night stay -- identically across 13 sampled runs after the ambiguity fix above.
  - "...checking in 2026-11-14 for one night" with a refrigerator and a microwave correctly
    extracted `amenityCode: ["REFRIGERATOR", "MICROWAVE"]` and `checkOutDate: "2026-11-15"`, and
    returned zero results -- confirmed against a direct `GET /availability` call that no seeded
    room type actually has both amenities together, so the empty result is correct, not a bug.
  - "I'm a AAA member..." extracted `rateCategory: "AAA_CAA"` and returned Harborview Grand's
    room types with the real 10%-off nightly rates (e.g. `$249.00` -> `$224.10`).
  - "A quiet room for two at Harborview Grand" (no dates at all) returned
    `400 VALIDATION_FAILED` with "I didn't catch your dates -- what check-in and check-out are
    you thinking?" and made **no** `GET /availability` call -- confirmed via the fake gateway's
    call log in the unit test and by inspection of this run.
- With `OPENAI_API_KEY` unset: a locally-run `hotelapp-ai serve` still started, `GET
  /assistant/health` reported `"status":"UP"` / `"provider":"NOT_CONFIGURED"`, and `POST
  /assistant/search` returned an ordinary `503 AI_UNAVAILABLE` Problem Details response.
- All 45 tests (11 new: 7 `services/search.py`, 4 `transport/rest/search.py`), `ruff check`, `mypy
  --strict`, and `lint-imports` pass (all four layering contracts still kept).

### A note on scope

This repository change was scoped to `hotelapp-ai-service` only, as AI Step 4 requires, plus one
one-line correction to `hotelapp-context/shared/api-contracts.md`'s `POST /assistant/search`
example (`"guests"` -> `"numGuests"`, matching `GET /availability`'s own parameter name used
everywhere else in that contract). No MCP, no OAuth, and no retrieval-graph changes were needed or
made.

## Step 3 status (the guest assistant, F3)

**Step 3 (the guest assistant, F3) is implemented in code.** The first step with a model call
that *generates* an answer -- corpus-grounded only, no backend calls, no reservation awareness --
per `../hotelapp-context/shared/phased-implementation-plan.md`.

Added in this step:
- `domain/errors.py` and `domain/assistant_events.py`: pure exception types and SSE event shapes,
  deliberately kept out of `services/assistant.py` so `transport/rest/assistant.py` can import the
  types it needs for `isinstance` checks without transitively pulling `httpx`/`psycopg` into
  `transport/`'s import graph -- import-linter's forbidden-module check is transitive, and this
  split is what keeps all four layering contracts green;
- `domain/citations.py`: the fixed `[n] Document Title -- Section` footnote format, unit-tested;
- `services/llm_client.py`: a raw `httpx` client for OpenAI chat completions -- streaming
  generation and structured-output (`json_schema`) classification for grading and query rewrite --
  mirroring `services/ingestion.py`'s existing embedding client rather than adding a second way to
  call the same API;
- `services/assistant.py`: the LangGraph retrieval graph (`rewrite_query -> retrieve -> grade ->
  [generate | decompose_and_retry, bounded to one retry] -> generate`), and
  `AssistantService.ask`, which streams citations then tokens then a `done` event -- or, for a
  phase-1 failure, raises before the first event so `transport/` can turn it into an ordinary
  Problem Details response instead of a truncated stream (error-handling.md §3);
- `transport/rest/assistant.py`: `POST /assistant/ask`, hand-rolled SSE framing (no new
  dependency), and the pre-stream/mid-stream error split, implemented by peeking the orchestrator's
  first event before opening the `StreamingResponse`;
- `prompts/rewrite_query.md`, `prompts/grade_retrieval.md`, `prompts/answer.md`;
- `hotelapp-ai ask "<question>"`: this step's manual-verification CLI, mirroring Step 2's
  `retrieve` exactly -- there is no frontend yet (item 9 is blocked on both copies of
  `ui-specifications.md` gaining assistant screens);
- `eval/smoke_questions.yaml`: 14 hand-written questions, eyeballed by hand on every change to
  retrieval, the prompts, or the corpus -- not scored, and not `eval/golden_set.yaml`, which is
  item 8's own, larger RAGAS-scored suite.

### A judgment call, flagged rather than silently applied

The architecture spec's diagram draws `dense_retrieve` and `sparse_retrieve` as two parallel
edges. `repositories/chunks.py#hybrid_search` already runs both halves in **one** SQL statement
(Step 2's explicit optimisation, load-bearing for the `EXPLAIN` test), and `services/
retrieval.retrieve_ranked_chunks` already composes embed -> hybrid search -> fuse -> rerank into
one call. The graph therefore has one `retrieve` node documenting what it does, rather than
re-splitting that into a fan-out that would either issue the query twice or fake a parallel edge
around a single result. Documented in `services/assistant.py`'s module docstring.

### A real finding from this step

OpenAI's newer chat-completion models (`gpt-5.4-mini`, `gpt-5.4-nano`) reject the `max_tokens`
parameter outright -- `400 Unsupported parameter: 'max_tokens' is not supported with this model.
Use 'max_completion_tokens' instead.` Found by hand running `hotelapp-ai ask` against the real
API while verifying this step. Fixed in `services/llm_client.py`, which sends
`max_completion_tokens` on the wire while keeping `max_tokens` as this module's own parameter name
(the project-wide term everywhere else).

**Verified in this environment, end to end, against the real Compose database, a running Spring
Boot backend, and a real `OPENAI_API_KEY`:**
- `uv run hotelapp-ai ask "Can I bring my dog?"` -- AI Step 2's troublesome pet-policy question --
  retrieved the Harborview Grand and Lakeside Inn pet-policy chunks and the Accessibility
  Commitment's service-animal section, graded the retrieval sufficient on the first pass, and
  generated a correctly-cited answer that distinguishes pets from service animals -- citing the
  real pet-policy chunks even though the cross-encoder still ranks an unrelated breakfast-hours
  chunk first, which is exactly the failure this step's grading step exists to catch.
- `uv run hotelapp-ai ask "If I'm checking in to Harborview Grand on 14 November, what is the
  exact cancellation deadline...?"` -- the 48-hour worked example -- returned "12 November at
  00:00 Eastern," matching `shared/acceptance-criteria.md` exactly.
- `uv run hotelapp-ai ask "Do you have an indoor swimming pool at Lakeside Inn?"` -- a genuinely
  out-of-corpus question -- triggered the bounded retry (`retryCount=1` in the log line) and then
  declined rather than inventing an answer, directing the guest to contact the property.
- `uv run hotelapp-ai ask "I'm a AAA member. Does that get me a discount at both properties?"`
  correctly distinguished Harborview Grand's 10% AAA/CAA discount from Lakeside Inn's lack of one.
- A real `curl -N` against a running `hotelapp-ai serve` streamed `citation` events, then `token`
  events, then a `done` event with real usage and cost figures, over actual SSE.
- With `OPENAI_API_KEY` unset: `GET /assistant/health` reports `"provider":"NOT_CONFIGURED"` while
  `"status":"UP"`, and `POST /assistant/ask` returns an ordinary `503 AI_UNAVAILABLE` Problem
  Details response -- before any byte of a stream, per error-handling.md §4.
- `docker build` succeeds with the new `langgraph` dependency; `docker run --network none` against
  the built image still imports `hotelapp_ai.main` successfully and resolves `prompts/*.md` from
  inside the image, proving the offline property holds with generation added.
- All 37 tests (12 new: 3 citation, 5 graph/retry-bound, 4 SSE framing), `ruff check`/`format
  --check`, `mypy --strict`, and `lint-imports` pass. The REST-only boundary grep over
  `repositories/` still finds nothing.

### A note on scope

This repository change was scoped to `hotelapp-ai-service` only, as AI Step 3 requires. No MCP, no
OAuth, no F2 natural-language search, and no changes to `hotelapp-context` were needed or made.

## Step 2 status (hybrid retrieval)

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
uv run hotelapp-ai ask "Can I bring my dog?"                     # Step 3: a cited, streamed answer
uv run hotelapp-ai search "A room for two, next weekend"         # Step 4: availability parameters
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
