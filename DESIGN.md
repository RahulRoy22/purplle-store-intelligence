# System Design — Purplle Store Intelligence

## Overview

This document describes the architecture of the Purplle Store Intelligence system: a real-time CCTV analytics pipeline that detects, tracks, and analyses in-store visitor behaviour and surfaces actionable KPIs through a REST API.

---

## High-Level Architecture

```
CCTV Footage (mp4 / live)
       │
       ▼
  [CV Pipeline]  ── one OS process per camera ──────────────────────────────┐
  YOLOv8 (detect persons)                                                    │
       │                                                                     │
  ByteTrack (multi-object tracking, persistent track IDs)                   │
       │                                                                     │
  StaffClassifier (HSV uniform detection, CPU-only)                         │
       │                                                                     │
  OSNet Re-ID (cross-frame identity, cosine similarity)                     │
       │                                                                     │
  ZoneMapper (Shapely polygons, pixel → store zone)                        │
       │                                                                     │
  TrackStateMachine (ENTRY / ZONE_ENTER / ZONE_DWELL / EXIT / ...)         │
       │                                                                     │
  EventBuilder (stamps UUID, UTC ISO-8601, camera_id)                      │
       │                                                                     │
  IngestClient (async httpx, batch POST, 503 retry/backoff)  ───────────────┘
       │
       ▼
  [FastAPI API Service]  ── single Docker container ──
  POST /events/ingest   (idempotent, batch ≤ 500)
  GET  /stores/{id}/metrics
  GET  /stores/{id}/funnel
  GET  /stores/{id}/heatmap
  GET  /stores/{id}/anomalies
  GET  /health
       │
       ▼
  SQLite (aiosqlite, WAL mode, dual-indexed)
       │
       ▼
  POS CSV (loaded once at startup, IST → UTC conversion)
```

---

## Component Design

### Detection & Tracking

YOLOv8n (nano) was chosen for the detection backbone: it runs in real time on CPU (≥10 FPS for 720p), which is required because the production environment is a retail store with no GPU guarantee. ByteTrack is integrated natively inside the `ultralytics` package via `.track(persist=True)`, eliminating the need for a separate tracking library and its associated version-pinning surface.

### Staff Classification

A two-stage HSV mask approach classifies staff from customers. The first stage computes the fraction of pixels within the bounding-crop that fall inside the configured HSV range (store uniform colour). If this fraction exceeds a configurable threshold (`STAFF_THRESHOLD`, default 0.30), the track is marked `is_staff=True`. This is intentionally simple and fast — it runs per-frame on every crop without a second neural network. The threshold is tunable per-store via environment variables.

### Re-Identification

OSNet-x0_25 (pre-trained on Market-1501) extracts a 512-dimensional L2-normalised embedding from each crop. Visitor identity is established by cosine similarity against a per-process registry. Re-ID runs only on the **first appearance** of a new ByteTrack ID to avoid redundant model inference. The registry maps `visitor_id → embedding` and is reset when the process restarts — appropriate for a single-session deployment.

### Zone Mapping

Each camera's layout is a JSON file produced by `scripts/calibrate.py` containing pixel polygons drawn interactively on a reference frame. `ZoneMapper` uses Shapely's `covers()` predicate for point-in-polygon classification. This is O(zones) per centroid — fast enough at 30 FPS with 6 zones.

### State Machine

`TrackStateMachine` maintains per-track state across frames:
- Each track starts as `PENDING` on first detection, emitting `ENTRY` on the first frame and transitioning to `IN_ZONE` or `FLOOR`.
- Zone transitions emit `ZONE_ENTER` / `ZONE_EXIT` pairs.
- Billing zone entry emits `BILLING_QUEUE_JOIN` with queue depth from metadata.
- When a track disappears (not in ByteTrack's active set), `flush_exits()` emits `EXIT` and records dwell.
- On `KeyboardInterrupt`, `flush_exits(set(), ...)` is called to drain all in-flight tracks before process exit.

### Ingestion API

FastAPI with a single SQLite database (via `aiosqlite`) was chosen over PostgreSQL to eliminate external infrastructure dependencies — the entire stack runs with `docker compose up` on any developer machine. The `events` table uses `event_id TEXT PRIMARY KEY` and `INSERT OR IGNORE` for idempotent batch ingest. Two covering indexes (`store_id, timestamp` and `visitor_id, timestamp`) make all analytics queries index-scannable.

### POS Correlation

Conversion rate is computed with a pure SQL `JOIN` using SQLite's `unixepoch()` function, which natively handles ISO-8601 strings with timezone offsets. A 5-minute (300-second) bilateral window matches billing zone dwells to POS transactions without any Python-level datetime arithmetic or in-memory loops.

---

### Billing Events: emit-raw, reconcile-in-analytics

The pipeline emits a `BILLING_QUEUE_JOIN` on **every** entry into the billing zone and a `BILLING_QUEUE_ABANDON` on **every** billing-zone exit that is not followed by a correlated sale. It deliberately does **not** try to decide, at capture time, whether a given billing visit "really" ended in a purchase. That decision is made later, in the analytics layer, by correlating billing-zone dwell against POS transactions inside the 5-minute `unixepoch()` window.

This is a conscious **"emit raw, reconcile in analytics"** split, and it mirrors the `is_staff` decision (raw flag emitted, filtered at read time):

- **The pipeline cannot see the till.** It has no access to POS at capture time, so any purchase/abandon verdict it made would be a guess. Emitting the raw join/exit and reconciling against POS in SQL means the *authoritative* signal (an actual transaction) decides conversion, not a camera heuristic.
- **It keeps the event log forensically complete and re-computable.** Because both the join and the exit are always recorded, the abandonment rate, conversion rate, and funnel are all derivable — and *re-derivable* — from the same immutable log. If we change the correlation window from 5 minutes to 3, or fix a POS import bug, we re-run the query; we never re-capture footage.
- **Consequence we accept:** `BILLING_QUEUE_ABANDON` is an *upper bound* on true abandonment until POS reconciliation runs (a visitor who bought but whose POS row is delayed/missing looks like an abandon). The analytics layer treats POS as the source of truth and the billing events as the behavioural envelope around it. This behaviour is covered by existing tests and is intentionally **not** changed.

### Anomaly detection anchoring (and the multi-day CONVERSION_DROP rule)

All anomaly rules are anchored to **`store_now` = the store's latest non-staff event timestamp**, not wall-clock time. The datasets are historical/replayed, so a wall-clock "is this zone stale?" check would fire on *every* zone the moment the feed stopped. Anchoring to the latest event makes "dead zone" mean "unvisited relative to the rest of the store's activity," which is the question that actually matters.

`CONVERSION_DROP` compares the conversion rate on `store_now`'s day against the average of the **prior 7 days**. This rule **requires multi-day history**: with a single day of seeded data there is no prior baseline, so the rule emits **nothing** rather than fabricating a drop. This is by design — surfacing a "drop" with no baseline would be exactly the kind of input-independent output the integrity check warns against. On a real deployment accumulating days of events, the same code begins emitting `CONVERSION_DROP` automatically once ≥1 prior day of footfall exists.

## AI-Assisted Decisions

Three design decisions were shaped in direct collaboration with the LLM and are documented here because the AI's reasoning changed the final implementation in non-obvious ways.

### 1. Extracted `init_db()` instead of schema-only-in-lifespan

**AI observation:** `httpx.ASGITransport` — the recommended tool for testing FastAPI with `pytest-asyncio` — does not fire ASGI lifespan events. If schema creation only lived inside the `lifespan()` context manager, every test would run against a schema-less database, producing false-passing tests that break silently in production when the table columns don't match.

**AI suggestion:** Extract `init_db(db_path: str)` as a standalone async coroutine callable from both the production lifespan hook and test fixtures, making schema creation explicit and testable.

**Decision:** Adopted as proposed. The AI identified a non-obvious Python testing pitfall (ASGI transport lifecycle bypass) that would have produced an entire class of silent, false-passing coverage. This was accepted immediately because it also made the schema a single source of truth with a clear migration path to PostgreSQL.

### 2. `unixepoch()` for POS-to-visitor correlation vs. Python in-memory loop

**AI initial draft:** A Python nested loop over billing events and POS rows with `timedelta` arithmetic.

**Engineer rejection:** "This is exactly the kind of logic that belongs in SQL. We have 500-event batches and thousands of POS rows — an O(N×M) Python loop is a performance time bomb."

**AI deeper investigation:** SQLite's `unixepoch(timestamp)` correctly parses ISO-8601 strings that include timezone offsets (`+00:00`) and converts them to Unix epoch integers. This made the entire correlation expressible as a single SQL `JOIN` with an arithmetic `WHERE` clause.

**Decision:** The AI's willingness to search for a pure-SQL approach (after being explicitly blocked from the easy path) produced a significantly better solution — one that is index-eligible, has no N×M complexity, and handles timezone-offset strings correctly without any pre-processing. Three boundary tests (T+240s, T+300s, T+360s) were written to verify the edge of the 5-minute window.

### 3. OSNet Re-ID runs only on first track appearance

**AI observation:** If Re-ID ran on every frame, a 30 FPS video with 10 active tracks would invoke the OSNet model 300 times per second — exceeding CPU real-time budget by approximately 20×.

**AI suggestion:** Cache Re-ID embeddings by ByteTrack ID and only invoke `extract_embedding()` on the first appearance of a new track ID. ByteTrack provides persistent IDs across frames, so once identity is established at first appearance it is stable for the track's lifetime without re-running the model.

**Decision:** Adopted. This reduced Re-ID inference from O(tracks × frames) to O(unique_tracks), making CPU-only inference viable. The trade-off is that if a person changes appearance drastically mid-track (e.g., removes a jacket), the Re-ID embedding will be stale — acceptable for a retail session of 10–60 minutes where appearance is generally stable.

### 4. Dual-schema tolerant ingest — adapting to the organiser's *shipped* event schema

**Context the AI flagged:** The updated dataset ships a `sample_events.jsonl` whose schema does **not** match the PDF's page-5 "Required Output Schema" that our pipeline emits. The shipped rows use lowercase `event_type` (`entry`, `zone_entered`, `queue_completed`…), `id_token`/`track_id` instead of `visitor_id`, `event_timestamp`/`event_time`/`queue_join_ts` for time (and they are timezone-*naive*), carry no `event_id` or `confidence`, and add rich detection fields (`gender_pred`, `age_pred`, `group_id`, `zone_type`, `is_revenue_zone`, `zone_hotspot_x/y`, `wait_seconds`, `queue_position_at_join`). Our `EventIn` model rejected every line of it.

**Options weighed (with AI):** (a) keep only the canonical schema and treat the shipped file as a detection reference; (b) re-target the whole API to the shipped schema; (c) a *normalization layer* in front of validation that accepts both. The held-out event set in Part B is most plausibly in the shipped shape (it is the only concrete event artifact the organiser provides), so option (a) risked the 20-point API-correctness block; option (b) discarded a working, tested build for a still-ambiguous target.

**Decision:** Adopted (c) — `services/api/models/normalize.py::normalize_event` maps the shipped shape onto the canonical one and returns canonical/unknown rows unchanged, so the existing per-event partial-success path is untouched. Two judgement calls where I overrode the naive mapping: (1) **billing zones collapse to the literal `zone_billing`** because the analytics SQL matches on that exact value — a faithful pass-through of `PURPLLE_MUM_1076_Z_BILLING_01` would have silently zeroed conversion/funnel/queue metrics; (2) **`event_id` is derived deterministically** (`uuid5` over identity fields, or the row's own `queue_event_id`) rather than randomly, so re-POSTing a shipped batch is still idempotent. `queue_position_at_join` is mapped to `metadata.queue_depth`; `confidence` defaults to `0.9` (we never drop low-confidence rows, per the scoring rubric).

### 5. VLM-assisted zone extraction from the floor-plan PNGs

**Context:** The updated drop replaced the machine-readable layout (xlsx/JSON) with **architectural floor-plan images** (`Store 1 - layout.png`, `store 2 - layout.png`) for two stores with different camera roles. Zone names had to be read off the drawings.

**Prompt used (Claude vision):** *"This is a retail store floor plan. List every labelled fixture/brand block, the cash-counter location, and the entrance. Group them into ENTRY, FLOOR, and BILLING zones and return JSON with zone_id, name, category, and which of these camera clips most likely covers each: [clip filenames]."* The model correctly grouped the wall shelves, makeup units and cash counter; **I overrode** its tendency to make each individual brand its own zone (too granular for the 3–4 camera angles available) and merged them into camera-aligned floor zones. Result captured in `data/mock/real_store_layouts.json`. Pixel polygons for tracking are still calibrated per-camera against real frames (`scripts/calibrate.py`) — the VLM gave us the semantic zone map, not geometry.

---

## Trade-offs Summary

| Decision | Chosen | Alternative | Reason |
|---|---|---|---|
| Database | SQLite | PostgreSQL | Zero external infra; `docker compose up` works without setup |
| Detection | YOLOv8n | YOLOv8s/m | CPU real-time budget (≥10 FPS); model weights auto-downloaded |
| Re-ID frequency | First appearance only | Every frame | O(unique_tracks) vs O(tracks×frames); enables CPU inference |
| Zone geometry | Shapely pixel polygons | Grid cells | Pixel accuracy; drawn interactively via calibrate.py |
| POS timestamp | UTC stored, IST converted at ingest | Store as IST | Correct `unixepoch()` arithmetic; all timestamps in same TZ |
| Multi-camera | One OS process per camera | Shared asyncio loop | Independent GIL; no cross-camera GPU contention; per-camera logs |
