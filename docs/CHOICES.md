# Architectural Choices & Trade-offs

This document records every non-obvious technical decision made during the build of the Purplle Store Intelligence System. For each choice the record covers: what was available, what the AI architect suggested, what I approved, and why the decision holds up under scrutiny.

---

## Phase 1 — API Architecture

### Decision 1: SQLite with `init_db()` extracted for testability

**Context**

The ingestion API needs a persistent store that is writable by both the FastAPI process and the Docker seed service, requires no external infrastructure, and must allow the test suite to run without a running Docker daemon.

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| PostgreSQL | Production-grade, full ACID, rich query planner | Requires a separate container, docker-compose dep, psycopg2 wheels — complicates zero-touch startup |
| SQLite (schema in lifespan only) | Simple | `httpx.ASGITransport` does NOT fire ASGI lifespan events; tests would silently run against a schema-less DB, giving false-passing coverage |
| In-memory SQLite per test | Fast isolation | Can't share state between fixture and handler; each `aiosqlite.connect()` call opens a new in-memory DB |
| **SQLite with `init_db()` extracted** | Testable without a server; single source of truth for DDL; swappable to Postgres in Phase 2 by changing one connection string | Requires one explicit `await init_db(path)` call in test fixtures |

**AI Suggestion**

The AI identified that `httpx.ASGITransport` bypasses the ASGI lifespan protocol — a non-obvious Python testing pitfall. It proposed extracting `init_db(db_path: str)` as a standalone async coroutine callable from both the production lifespan hook and test fixtures, so tests could bootstrap the schema explicitly without a running server.

**Approved by:** me, for the following reasons:
- Keeps the acceptance gate (`docker compose up`) truly zero-touch: the lifespan still runs `init_db` on production startup.
- Makes tests deterministic: no implicit dependency on whether or not the ASGI startup lifecycle ran.
- The DDL lives in exactly one place (`main.py`) — there is no risk of schema drift between test and production.
- Migration path to Postgres is trivially `s/aiosqlite.connect/asyncpg.connect/` with no DDL changes.

**Decision status:** Stable. This pattern will be extended in Phase 2 (ingest) and Phase 3 (analytics queries) without modification.

---

### Decision 2: Python `urllib.request` healthcheck instead of `curl` in Docker

**Context**

The `docker-compose.yml` `api` service requires a `healthcheck` so Docker's dependency engine knows when the API is ready before marking it `healthy`. The standard idiom is `curl -f http://localhost:8000/health`.

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| `curl` | One-liner, industry standard | Not present in `python:3.11-slim`; installing it adds ~3 MB and a non-Python dependency layer |
| `wget` | Slightly smaller than curl | Also not in slim, same layer cost |
| Custom shell script | Flexible | Extra file, same layer cost problem |
| **`python -c "import urllib.request; ..."` (stdlib)** | Zero extra dependencies; already in the image; self-documents the API URL | Slightly more verbose than `curl -f` |

**AI Suggestion**

The AI flagged that `python:3.11-slim` deliberately strips non-essential binaries (including `curl` and `wget`) to minimise attack surface. It recommended the stdlib `urllib.request` one-liner so we never break the zero-touch gate by pulling in a system package that might not resolve in air-gapped or corporate environments.

**Approved by:** me, for the following reasons:
- Eliminates an entire class of "healthcheck silently broken because curl missing" CI failures.
- No `RUN apt-get install curl` layer means the image stays slim, reproducible, and fast to pull.
- The acceptance gate requirement is "docker compose up must start everything cleanly without manual steps" — adding a system package install is a manual step risk.
- The Python stdlib has been stable for this use case for 15+ years.

**Decision status:** Stable. Will remain unchanged through all phases.

---

## Phase 2 — Ingestion Engine

*(To be completed after Phase 2 implementation)*

---

## Phase 3 — Business Logic & Intelligence API

*(To be completed after Phase 3 implementation)*

---

## Overarching Principles

1. **Zero-touch startup is non-negotiable.** Every architectural decision is evaluated against "does this break `docker compose up`?"
2. **Staff events are stored, never filtered at ingest.** Filtering happens exclusively at query time (Phase 3). This keeps the raw event log forensically complete.
3. **Idempotency via database semantics, not application logic.** `INSERT OR IGNORE` on `event_id PRIMARY KEY` means idempotency is guaranteed even if the application crashes mid-batch.
4. **Structured 503s, not stack traces.** Any database failure surfaces as `{"error": "database_unavailable", "trace_id": "..."}` — never a Python traceback — so downstream callers can implement exponential backoff without parsing HTML error pages.
