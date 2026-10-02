# Copilot instructions — hotelapp-ai-service

## What this repository is

The **AI service** for HotelApp, a hotel booking and guest-management demo built as a portfolio
piece. Target: **Python 3.13, FastAPI, LangGraph, pgvector, PostgreSQL 18.6**. It provides an MCP
server, a retrieval-augmented guest assistant, and natural-language availability search.

HotelApp is deliberately built as **two frontends and two backends against one REST contract**.
This service is a **fifth consumer of that same contract** — it is backend-agnostic exactly as the
frontends are, and must work unmodified against either backend.

> ## Read this before writing any code
>
> **This repository is empty.** There is no `pyproject.toml`, no source tree, no tests, and no
> lint config yet.
>
> That is deliberate. This project was specified first: **71 specification documents** live in the
> sibling repository `hotelapp-context`, and they are normative. Your first job on any task is to
> read the relevant ones, listed below — not to start scaffolding from habit. **Ten of those
> documents are this repository's own**, in `stacks/ai-service/`, and they already decide the
> folder layout, the layering rules, the dependency set, the error codes, and the test strategy.
>
> Because nothing is built yet, this file cannot document build or test commands. That section
> says "not yet established" and is honest about it. **It will be filled in, and validated by
> actually running them, once the first code exists.**

## The one thing that makes this repo unusual

> **This service cannot query business data.** Not "should not" — cannot, and must not learn how.
>
> - **The REST contract is the only way in.** There is no SQL access to `reservations`,
>   `properties`, `room_types`, `users` or any other business table, in any form. The database
>   role this service connects with has **no grant** on them, and CI greps `repositories/` for
>   those names.
> - **This service owns `ai_documents`, `ai_chunks`, `ai_eval_runs` and nothing else.** Anything
>   prefixed `ai_` is its own; everything else arrives over HTTP.
> - **Pricing, availability, cancellation deadlines and allocation are never recomputed here.**
>   They are fetched from endpoints that already compute them under test. An AI layer that
>   computes its own availability can hallucinate a room that does not exist, and no amount of
>   prompt engineering fixes a wrong number computed correctly from the wrong source.
> - **Flyway is still the sole DDL executor.** The `ai_*` tables arrive in
>   `V002__ai_tables.sql`, authored in `hotelapp-context`. This service **never creates schema** —
>   it asserts at startup that the tables and the `vector` extension exist and fails fast if not.
>
> Full reasoning: `shared/ai-enablement-overview.md` §3, and `stacks/ai-service/architecture-specification.md`.

## Required reading, in `../hotelapp-context/`

All repositories are opened together by `hotelapp.code-workspace`, so these paths resolve in this
workspace. **Read `context-map.md` first — it is the index and says which document decides what.**

| Read | When |
|------|------|
| `context-map.md` | Always, first. The index |
| `shared/ai-enablement-overview.md` | **The most important shared document for this repo.** The AI feature set, this service's boundary, the authorization model, the MCP design, retrieval, the corpus, evaluation |
| `shared/project-overview.md` | To know whether something is in scope |
| `shared/api-contracts.md` | **Normative.** Every endpoint this service consumes, and the ones it adds |
| `shared/data-model.md` | The schema — including what this service may **not** read |
| `shared/security-principles.md` | Before touching auth, secrets, or anything a guest can influence |
| `shared/decision-log.md` | Before proposing a change to a fixed decision — it may already have been reversed once |
| `shared/glossary-of-conventions.md` | Before naming anything |

**`stacks/ai-service/` holds this repository's own ten specification documents**:
`architecture-specification.md`, `coding-standards.md`, `testing-standards.md`,
`error-handling.md`, `security-implementation.md`, `logging-observability.md`,
`environment-setup-guide.md`, `devops-pipeline.md`, `dependency-policy.md`, `module-registry.md`.

## Stay in this stack

This workspace contains six repositories. Four implement the **same REST contract in different
technologies**, and their specification documents deliberately look similar — which makes reading
the wrong one an easy and quiet mistake.

- **Your specifications are `stacks/ai-service/` and nothing else.** Never read another stack's
  `stacks/` document for implementation guidance.
- `shared/` applies to every repository. `stacks/` applies to exactly one.
- **The specific trap:** `stacks/nodejs/error-handling.md` and `stacks/springboot/error-handling.md`
  are **byte-identical through section 5**. The two frontends' `ui-specifications.md` are identical
  through §2, and both backends' `testing-standards.md` through §5. So the wrong file reads
  correctly for most of its length and then hands you the wrong implementation. **Check the path
  before trusting a section.** `stacks/ai-service/` is in none of those pairs and is held to no
  byte-equivalence.
- **Never write source code into another repository's folder.** If a task seems to need a change
  in a sibling repository, say so and stop — that is a separate task in a separate repo.

## This repo's specifics

- **Four layers, strictly:** `transport/` → `services/` → (`gateways/` | `repositories/`), plus a
  pure `domain/`. The import rules are in `module-registry.md` and are **enforced in CI by
  import-linter**, not by good intentions.
- **`gateways/hotelapp.py` is the only module that may call a backend.** It forwards the caller's
  session cookie verbatim and **has no code path that adds credentials**. It is the most
  security-critical module here.
- **`repositories/` is the only module that may emit SQL**, and only against `ai_*` tables. No ORM
  — psycopg 3 with bound parameters.
- **`services/` is the only layer that may construct a prompt or call a model.** A model call in a
  route or a repository is a layering violation and makes the service untestable without a key.
- **`domain/` is pure** — RRF fusion, chunk boundaries, citation rendering. No I/O, no clock, no
  model. Same discipline as `domain/` in both backends.
- **Async everywhere.** `requests` is banned; one blocking call stalls the event loop.
- **Prompts are files in `prompts/`**, never string literals in service modules.
- **Untrusted text is fenced and labelled as data**, never interpolated into instructions.

## Fixed decisions — do NOT re-derive, re-propose, or "improve"

These were decided with reasoning, and several were decided *after* being reversed once. Changing
any of them means changing documents in six repositories.

| Decision | Detail |
|----------|--------|
| PostgreSQL | **18.6 specifically.** The schema uses `uuidv7()`, a PostgreSQL 18 core function; 17 will not apply the migrations |
| Auth | **Server-side sessions only.** Opaque token, SHA-256 hashed, `HttpOnly; Secure; SameSite=Lax; Path=/api/v1`. 8h sliding idle, 30d absolute cap |
| Auth — what is banned | **No JWTs. No refresh tokens. No `Authorization: Bearer` issued by this project. No `/auth/refresh`.** That design was specified and then reversed — `decision-log.md` entry 2. **OAuth 2.1 for MCP uses opaque tokens for exactly this reason**; a JWT library in `pyproject.toml` means someone is re-litigating a reversal |
| Errors | RFC 9457 Problem Details. Clients branch on `code`, never on `detail` |
| No overbooking | Enforced by a PostgreSQL **exclusion constraint**, not application logic — and never by this service |
| Money | `numeric(10,2)` → decimal **string** in JSON. **Never** a float, at any layer. `float("249.00")` in this repo is a defect |
| Migrations | **Flyway is the sole DDL executor.** No Alembic, no ORM-managed schema |
| Vector storage | **pgvector in the existing PostgreSQL.** No Pinecone, Weaviate, Qdrant, or Chroma |
| Orchestration | **LangGraph only.** No AutoGen, no CrewAI, no LlamaIndex |
| MCP | The server is a **pure OAuth 2.1 resource server** (2026-07-28 spec), never an authorization server. It validates tokens; it never issues one |
| Hosting | **None.** Local dev, Docker Compose. No deployment, no cloud |

**`api-contracts.md` is normative.** Where this code and that document disagree, the code is wrong.

## Conventions that apply to every file you write

- **Casing across layers** is fixed in `glossary-of-conventions.md`: `snake_case` in SQL and
  Python, `camelCase` in JSON, `kebab-case` in URLs, `SCREAMING_SNAKE_CASE` for enum values — and
  **enum values cross every layer unchanged**, including into prompts and tool schemas.
- **Pydantic models at every boundary**, with a camelCase alias generator and `extra="forbid"`.
  Hand-written `dict` payloads at a route boundary are prohibited.
- **Dates carry their type in the name**: `_date` for calendar dates, `_at` for timestamps. A
  naive `datetime` crossing a module boundary is a defect.
- **Type hints are mandatory**; `mypy` runs strict and fails CI.
- **Commits** follow Conventional Commits. A **prompt change is `feat:` or `fix:`, never
  `chore:`** — it changes behaviour. A commit that moves a RAGAS floor states the before and after
  numbers in the body.
- **Commit AND push at the end of every step, once verification passes.** This rule was added to
  three sibling repos mid-project after each shipped verified work that sat uncommitted; this repo
  starts with it in place.
- **Any new pure function with non-obvious logic ships with unit tests in the same commit** —
  especially in `domain/`, where determinism is the whole point.
- Default branch is `main` in all repositories.

## A green test suite is NOT sufficient here

This is the one place this repository departs from its four siblings, and it matters.

Everywhere else in this project, passing tests mean a change is safe. **Here they do not.** A
model, embedding, prompt or framework change can leave every test green while answer quality
drops, and nothing in pytest can see it. There are **two independent gates**:

- `pytest` — logic, wiring, SQL, auth, protocol. Everything deterministic.
- **The RAGAS evaluation suite** over `eval/golden_set.yaml`, with committed floors.

An upgrade that moves a floor is a failed upgrade, whatever the unit tests say. See
`stacks/ai-service/testing-standards.md` and `dependency-policy.md`.

## Build, test, and lint — NOT YET ESTABLISHED

There is nothing to build yet, so there is nothing to document. **Do not invent commands and do
not assume a conventional scaffold exists.**

When you create the project, follow `stacks/ai-service/environment-setup-guide.md` in
`hotelapp-context` — it specifies the intended commands, the `.env` contents, and the
configuration mechanism. Those commands are **specified but have never been run**, so treat them
as a plan to validate rather than as verified fact.

Two things known in advance to bite:

- **The database must be `pgvector/pgvector:pg18`, not `postgres:18.6`.** Official Postgres images
  ship no third-party extensions, so `CREATE EXTENSION vector` fails with the control file simply
  absent.
- **The API must mount under `/api/v1/`.** The session cookie is `Path=/api/v1`, and cookie scope
  is host plus path and ignores port — mount it elsewhere and pass-through authorization silently
  stops working with no error explaining why.

**Once the first code exists, rewrite this section** with commands that have actually been
executed, in the order that works, including any errors and their workarounds.

## Trust these instructions

**Trust this file first.** Search the codebase only when the information here is incomplete, or
when you find it to be in error. If you do find an error, say so — do not quietly work around it.

The specification documents named above are the authority on *what* to build. This file is the
authority on *where to look*. Neither is a substitute for reading the other.
