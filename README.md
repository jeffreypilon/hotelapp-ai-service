# hotelapp-ai-service

Python/FastAPI AI service for HotelApp: an MCP server, a retrieval-augmented guest assistant, and
natural-language availability search. Reads business data over the REST contract, never SQL.

## Status

**Step 0 (walking skeleton) is done**: FastAPI app, `GET /api/v1/assistant/health`, one real REST
call to a backend (`gateways/hotelapp.py`), Compose wiring under `--profile ai`, and CI (lint,
format, mypy strict, `lint-imports` layering, the REST-only grep, unit tests, image build). No
retrieval, no model calls, no MCP, no database wiring yet -- see
`../hotelapp-context/shared/phased-implementation-plan.md`'s "AI Step 0" for the authoritative
scope and `stacks/ai-service/architecture-specification.md` for the full design.

The canonical setup guide is
`../hotelapp-context/stacks/ai-service/environment-setup-guide.md`. This section records what was
**actually run and verified** for Step 0, since that document itself was not updated as part of
this change (see "A note on scope" below).

## Clone to running, as verified for Step 0

```powershell
# uv is not assumed pre-installed
irm https://astral.sh/uv/install.ps1 | iex
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"   # add to PATH each new terminal session

uv python install 3.13   # if not already present -- uv manages its own interpreter
uv sync --frozen          # NOT `uv add` -- respects the committed uv.lock
cp .env.example .env      # then edit; OPENAI_API_KEY absent is a valid, supported state

uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run lint-imports
uv run hotelapp-ai serve   # FastAPI on :8000
```

```powershell
curl http://localhost:8000/api/v1/assistant/health
```

**Verified, with no `OPENAI_API_KEY` set anywhere:**
- The service starts cleanly and `/health` reports `"provider": "NOT_CONFIGURED"`.
- With a real backend reachable, `/health` reports `"backend": "UP"`; stopping that backend (and
  only that) flips the same field to `"DOWN"` -- the two states are independent, as designed.
- Setting `OPENAI_API_KEY` flips `"provider"` to `"UP"` with no other change.
- All four existing Phase 8 Compose combinations (`react`/`angular` x `spring`/`node`, no `ai`
  profile) still come up and serve `GET /properties` correctly against the `pgvector/pgvector:pg18`
  database image, with no API key anywhere.
- `docker compose --profile react --profile spring --profile ai up --build` brings up all four
  services together; `lint-imports` and the `repositories/` grep both pass in CI while trivially
  true (no files in `repositories/` yet).

### A real finding from this step

Now that `V002__ai_tables.sql` exists in `shared/migrations/`, Flyway applies it **unconditionally**
against any database it migrates -- not just when `--profile ai` is used. A native (non-Docker)
PostgreSQL 18 install with no pgvector extension therefore fails `CREATE EXTENSION vector` and
**Spring Boot refuses to start at all** against it, even for plain Phase 1-8 work. Confirmed
directly on this machine. Compose is unaffected once `db`'s image is the pgvector build (this
step's own required change); native local development now needs pgvector installed into the native
Postgres too, project-wide -- not only for engineers working on this service.

### A note on scope

The Step 0 instructions in `phased-implementation-plan.md` ask for
`stacks/ai-service/environment-setup-guide.md` to be rewritten with what actually worked, as the
last act of this step. That file lives in `hotelapp-context`, and this change was scoped to touch
only `hotelapp-ai-service` plus `hotelapp-context/docker/docker-compose.yml` -- so that rewrite
has **not** been done, and the canonical guide still reads as an unvalidated plan. This section
exists so the verified commands are recorded somewhere; the context repo's own document still
needs the same update next time it's in scope.

