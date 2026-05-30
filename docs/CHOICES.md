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

### Decision 3: Query-before-insert for accurate duplicate counting

**Context**

`POST /events/ingest` must return `{"accepted": N, "duplicate": M}`. SQLite's `INSERT OR IGNORE` silently skips duplicate `event_id` rows, but its DBAPI cursor does not reliably expose how many rows were actually skipped versus inserted when using `executemany` across Python versions.

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| `cursor.rowcount` after `executemany` | No extra query | SQLite C library reports rowcount per-statement; Python's aiosqlite can aggregate differently across versions — unreliable for `executemany` |
| Two-pass: `INSERT OR REPLACE` + compare | Deterministic | `REPLACE` is a DELETE+INSERT, destroying `ingested_at` timestamps and invalidating audit logs |
| **Query existing IDs → compute sets → INSERT OR IGNORE** | Accurate counts regardless of driver version; explicit; testable | One extra SELECT per batch — negligible at ≤500 events |

**AI Suggestion**

The AI identified the `cursor.rowcount` unreliability under `executemany` (a known CPython/aiosqlite footgun) and proposed the query-before-insert pattern: `SELECT event_id FROM events WHERE event_id IN (…)` to build the duplicate set, then `INSERT OR IGNORE`, then `inserted = len(valid) - len(duplicates)`.

**Approved by:** me, for the following reasons:
- The accepted/duplicate split in the response is a first-class API contract, not a debugging hint — correctness matters.
- One extra SELECT on ≤500 UUIDs is microseconds; the latency budget is not affected.
- The pattern is explicit and testable: `test_ingest_partial_duplicate` verifies exact counts, which would be impossible to assert if we relied on driver internals.

**Decision status:** Stable. Implemented in `db/events.py::batch_upsert`.

---

## Phase 3 — Business Logic & Intelligence API

### Decision 4: Pure SQL POS correlation with `unixepoch()` — engineer overrule of AI's initial suggestion

**Context**

The conversion metric requires correlating a visitor's billing zone dwell with a POS transaction that occurred within 5 minutes of that dwell. The challenge: timestamps are stored as ISO-8601 strings (e.g., `2026-05-30T10:04:00+00:00`), and the 5-minute window arithmetic must be both correct and executed without loading rows into Python.

**The AI's Initial Suggestion (overruled)**

The AI's first proposal was a Python in-memory loop:

```python
# AI draft — rejected
for visit_ts, visitor_id in billing_visits:
    for txn_ts in pos_transactions:
        delta = (txn_ts - visit_ts).total_seconds()
        if 0 <= delta <= 300:
            converted.add(visitor_id)
```

The rationale offered was a concern that SQLite's `strftime`/`julianday` functions might mishandle timezone-offset strings (`+00:00`) and produce incorrect deltas, making a Python loop safer to reason about.

**Engineer Overrule**

The engineer rejected this approach on architectural grounds:

> "This is exactly the kind of logic that belongs in SQL, not Python. We have 500-event batches and thousands of POS rows — an O(N×M) Python loop is a performance time bomb. Find the SQL solution."

This constraint forced a deeper investigation of SQLite's time functions.

**Resolution: `unixepoch()` handles timezone-offset strings natively**

SQLite's `unixepoch(timestamp)` function correctly parses ISO-8601 strings that include timezone offsets and converts them to Unix epoch integers before subtraction — eliminating both the timezone ambiguity concern and the need for any Python datetime arithmetic:

```sql
SELECT COUNT(DISTINCT e.visitor_id)
FROM   events           e
JOIN   pos_transactions p ON p.store_id = e.store_id
WHERE  e.store_id  = ?
  AND  e.is_staff  = 0
  AND  e.zone_id   = 'zone_billing'
  AND  unixepoch(p.timestamp) >= unixepoch(e.timestamp)
  AND  unixepoch(p.timestamp) -  unixepoch(e.timestamp) <= 300
```

The solution was verified with three boundary tests:
- `T+240s` (within window) → converted ✓
- `T+360s` (outside window) → not converted ✓
- `T+300s` (exact edge, inclusive) → converted ✓

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| Python in-memory loop (AI draft) | Easy to debug per row | O(N×M) complexity; loads all rows; can't benefit from DB indexes; violates principle of pushing computation to the data |
| `julianday()` arithmetic | Works for UTC timestamps | Does not correctly handle `+00:00` timezone suffixes without pre-processing |
| Store timestamps as Unix integers at ingest | Fast arithmetic | Breaking schema change; loses human-readable timestamps in audit logs |
| **`unixepoch()` in JOIN condition (final)** | Pure SQL; O(1) per row with index on `(store_id, timestamp)`; handles timezone offsets natively | Requires SQLite ≥ 3.38 (2022-02-22) — universally available in Python 3.11+ |

**Approved by:** me. The engineer's insistence on a pure SQL architecture directly produced a better solution: fewer moving parts, no Python-level join, and index-eligible execution.

**Decision status:** Stable. The 5-minute window constant (300 s) is derived from the domain spec and has no magic numbers in the query — it is the exact boundary the tests validate.

---

## Overarching Principles

1. **Zero-touch startup is non-negotiable.** Every architectural decision is evaluated against "does this break `docker compose up`?"
2. **Staff events are stored, never filtered at ingest.** Filtering happens exclusively at query time (Phase 3). This keeps the raw event log forensically complete.
3. **Idempotency via database semantics, not application logic.** `INSERT OR IGNORE` on `event_id PRIMARY KEY` means idempotency is guaranteed even if the application crashes mid-batch.
4. **Structured 503s, not stack traces.** Any database failure surfaces as `{"error": "database_unavailable", "trace_id": "..."}` — never a Python traceback — so downstream callers can implement exponential backoff without parsing HTML error pages.
