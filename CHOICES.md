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

## Phase 4 — CV Pipeline

### Decision 5: YOLOv8n (nano) as the detection backbone

**Context**

The pipeline has to detect people in 1080p CCTV at roughly 15 FPS, on a retail back-office box with **no GPU guarantee**. Detection is the per-frame hot path: it runs on every frame of every camera, so its cost sets the ceiling for how many cameras one machine can serve. I needed the smallest model that still detects partially-occluded, small-in-frame people reliably enough for tracking to stay locked on.

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| **YOLOv8n** | ~3.2M params; real-time on CPU (~15–25 FPS @ 640 on a modern x86 core); weights auto-download; ByteTrack ships in the same `ultralytics` package | Lower mAP than larger variants on small/occluded persons |
| YOLOv8s | +6–8 mAP over nano | ~3× the FLOPs — drops well below real-time on CPU once you run 5 cameras |
| YOLOv9 / RT-DETR | State-of-the-art accuracy | Heavier; RT-DETR is transformer-based and effectively needs a GPU for our FPS; more dependency/version surface |
| MediaPipe Pose/Detector | Very fast, CPU-friendly | Tuned for close-range single-subject (selfie/fitness), not multi-person wide-angle CCTV; poor at small/occluded people |

**What the AI suggested**

The AI initially reached for YOLOv8s "for better accuracy," reasoning that retail occlusion is hard and a bigger model would reduce missed detections.

**What I chose, and why**

I overruled it and picked **YOLOv8n**. The deciding factor is the deployment constraint, not the benchmark leaderboard: we run **five cameras** on a CPU-first box. v8s would force either a GPU requirement (breaking the "runs anywhere" promise) or a frame-skip that hurts ByteTrack's continuity more than nano's lower mAP does. Two design choices buy back nano's accuracy gap:

- **Tracking smooths detection noise.** ByteTrack associates across frames using both high- and low-confidence boxes, so a person missed in one frame is recovered from the track — we don't need a perfect per-frame detector, we need a *consistent* one.
- **A deliberately low confidence threshold.** I run detection at conf ≈ 0.25 (classes filtered to COCO person only). On CCTV I would rather over-detect and let ByteTrack + the Re-ID gate reject spurious boxes than miss a real visitor and undercount footfall — undercounting directly corrupts the unique-visitor and funnel metrics the API exists to report.

If a GPU becomes available per store, the model name is a single env var (`YOLO_WEIGHTS=yolov8s.pt`) — nothing else changes. So this is a reversible default tuned for the worst-case (CPU) environment, not a one-way door.

**Decision status:** Stable. Default `yolov8n.pt`, conf ≈ 0.25, person-class only, ByteTrack via `ultralytics .track(persist=True)`.

---

### Decision 6: One event per zone transition, with `is_staff` emitted (not suppressed)

**Context**

The pipeline turns continuous tracks into a discrete event stream the API can aggregate. I had to decide the *grain* of that stream (how often we emit, and on what), and how to represent things the pipeline can infer but isn't certain about (staff vs. customer).

**Alternatives Considered**

| Option | Upside | Downside |
|---|---|---|
| **One event per zone transition** (ENTER/DWELL/EXIT, plus ENTRY/EXIT/billing) | Compact; each row is a meaningful state change; trivially aggregatable in SQL | Needs a per-track state machine to debounce jitter at zone borders |
| One event per frame | No state machine; raw fidelity | ~15 rows/sec/person — millions of rows/day; pushes all aggregation cost downstream; mostly redundant |
| Periodic snapshots (every N s) | Bounded volume | Misses fast transitions; arbitrary N; still redundant while a person stands still |

**What the AI suggested**

The AI proposed filtering staff out **inside the pipeline** ("don't even emit staff events — the API only cares about customers") and emitting one event per frame for "maximum downstream flexibility."

**What I chose, and why**

I rejected both halves and settled on **one event per zone transition, with staff emitted and filtered at the API**. The rationale:

- **Grain = transition.** A visitor's behaviour *is* a sequence of zone transitions (entered skincare, dwelled 4 min, left, joined billing). Emitting on transition makes every row semantically meaningful and lets the API answer dwell/heatmap/funnel questions with plain `GROUP BY` — no windowing over frame spam. The `TrackStateMachine` debounces border jitter so we don't emit ENTER/EXIT flicker.
- **`is_staff` is emitted, never suppressed.** Staff classification (HSV) is a *heuristic and will be wrong sometimes*. If the pipeline drops staff events, a misclassified customer is **silently and permanently lost** from every metric, and there's no way to audit or correct it. By emitting the flag and filtering with `WHERE is_staff = 0` at query time, the raw log stays forensically complete: we can re-tune the classifier, re-run analytics, or expose a staff-inclusive view later without re-ingesting anything. Classification confidence and filtering policy belong at the analytics edge, not baked irreversibly into capture.
- **`session_seq`** disambiguates a returning visitor: when Re-ID matches someone who already left, the new visit carries `session_seq = 2, 3, …` so REENTRY is distinguishable from a continuous session while `COUNT(DISTINCT visitor_id)` still counts the person once.
- **`zone_id` is null for ENTRY/EXIT** by design: those events mark the store boundary (the turnstile/door), which is not a merchandising zone. Forcing a sentinel zone there would pollute heatmap/dwell aggregates; `NULL` lets the zone queries ignore them naturally (`WHERE zone_id IS NOT NULL`).
- **Metadata is an open block.** `metadata` carries `queue_depth`, `sku_zone`, etc. as JSON with `extra="allow"`, so new signals (e.g. shelf-interaction counts) slot in without a schema migration or breaking existing callers.

**Decision status:** Stable. Implemented across `state_machine.py` (transition synthesis) and the API's read-time `is_staff = 0` filter.

**Addendum — accepting the organiser's *shipped* schema (dual-schema ingest)**

The updated dataset's `sample_events.jsonl` does not use the canonical schema above — it uses lowercase event types, `id_token`/`track_id`, naive timestamps, and rich detection fields (`gender_pred`, `age_pred`, `group_id`, `zone_type`, `is_revenue_zone`, `zone_hotspot_x/y`, `wait_seconds`, `queue_position_at_join`). Because the Part-B held-out event set is most plausibly in *that* shape, the schema decision is no longer just "what we emit" but "what we accept."

- **Options:** (a) canonical-only, treat the shipped file as reference; (b) re-target the whole API to the shipped schema; (c) a normalization adapter accepting both. I chose **(c)** — `models/normalize.py::normalize_event` — because (a) risked the 20-point ingest block on the organiser's own example file, and (b) threw away a tested build for a still-ambiguous target. The AI initially leaned toward (b) ("just match their file"); I **overrode** it: a thin, pure normalizer in front of `EventIn` de-risks both interpretations at a fraction of the blast radius, and leaves `batch_upsert`, the 207 partial-success path, and the analytics SQL untouched.
- **Two non-obvious mapping rules:** billing zones (e.g. `PURPLLE_MUM_1076_Z_BILLING_01`) **collapse to the literal `zone_billing`** the analytics match on — a faithful pass-through would have silently zeroed conversion/funnel/queue; and `event_id` is **derived deterministically** (`uuid5`, or the row's own `queue_event_id`) so re-POSTing a shipped batch stays idempotent rather than inflating counts. `queue_position_at_join → metadata.queue_depth`; missing `confidence → 0.9` (never dropped).

---

### Decision 7: Staff classifier is a fixed HSV uniform mask — a deliberate, replaceable shortcut

**Context**

Staff must be separable from customers so they don't inflate footfall and dwell. The brief's footage has **varying lighting**, which is exactly the condition under which colour-based classification is weakest.

**The trade-off I took (and its limits)**

The classifier flags a crop as staff when the fraction of pixels inside a fixed HSV range (the store's blue uniform, H ≈ 100–130) exceeds `STAFF_THRESHOLD`. I chose this knowingly as a **fast, dependency-free, CPU-only first cut**, not as the final answer:

- **Why it's acceptable now:** it adds zero model-load cost, runs per-crop in microseconds, is fully tunable per store via env vars, and a uniform is genuinely the strongest single cue for retail staff.
- **Where it breaks:** white/quartz-halogen vs. warm LED lighting shifts measured hue; a customer in a blue jacket false-positives; a staffer holding stock in front of their torso false-negatives. A *fixed* HSV band cannot adapt to these.
- **How I'd evaluate/replace it (likely follow-up):** first, **measure** — hand-label a few hundred crops across the lighting conditions in the footage and track precision/recall, because we currently have no number for how often it's wrong. Cheap robustness wins: convert to **HSV and key on Hue/Saturation only** (drop Value) to reduce brightness sensitivity, or **white-balance/normalise** each crop first. The principled replacement is a **small supervised classifier** (a tiny CNN head, or logistic regression on a colour histogram + the OSNet embedding we already compute) trained on those labels — it learns the uniform under real lighting instead of us guessing a band. Crucially, because **Decision 6 emits `is_staff` rather than suppressing it**, we can swap the classifier and re-run analytics on the existing event log with no re-capture.

**Decision status:** Intentional MVP. The HSV bounds and threshold are env-configurable (`STAFF_HSV_LOWER/UPPER`, `STAFF_THRESHOLD`); the upgrade path is measurement-first, then a learned classifier.

---

## Overarching Principles

1. **Zero-touch startup is non-negotiable.** Every architectural decision is evaluated against "does this break `docker compose up`?"
2. **Staff events are stored, never filtered at ingest.** Filtering happens exclusively at query time (Phase 3). This keeps the raw event log forensically complete.
3. **Idempotency via database semantics, not application logic.** `INSERT OR IGNORE` on `event_id PRIMARY KEY` means idempotency is guaranteed even if the application crashes mid-batch.
4. **Structured 503s, not stack traces.** Any database failure surfaces as `{"error": "database_unavailable", "trace_id": "..."}` — never a Python traceback — so downstream callers can implement exponential backoff without parsing HTML error pages.
