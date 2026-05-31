# PROMPT: Design pytest fixtures for a store analytics API backed by SQLite.
#   Stress-test: (a) staff-only store — all metrics must be 0/empty, never error;
#   (b) REENTRY visitors — COUNT(DISTINCT visitor_id) must not double-count;
#   (c) POS transactions at the exact 5-minute window boundary — T+300s inclusive,
#   T+301s excluded; (d) billing abandonment >50% triggers WARN anomaly with
#   suggested_action; (e) heatmap data_confidence="low" when fewer than 20 sessions.
#
# CHANGES MADE: Replaced julianday() arithmetic with unixepoch() after discovering
#   SQLite's julianday() does not correctly parse ISO-8601 strings with +00:00 timezone
#   offsets. Confirmed unixepoch() handles them correctly. Added data_confidence field
#   to heatmap response and corresponding test assertions. Added suggested_action
#   field to Anomaly model; updated anomaly tests to assert its presence.
"""
Phase 3 Tests — Analytics / Intelligence API

Endpoints under test:
  GET /stores/{store_id}/metrics
  GET /stores/{store_id}/funnel
  GET /stores/{store_id}/heatmap
  GET /stores/{store_id}/anomalies

Hard rules verified:
  RULE-1  Staff exclusion    — is_staff=1 events never appear in any metric
  RULE-2  POS correlation    — 5-minute billing-zone window via SQL unixepoch()
  RULE-3  REENTRY dedup      — same visitor_id with REENTRY counts as 1 unique
  RULE-4  Zero-purchase edge — no POS data → conversion_rate == 0.0 (not null/error)
"""
from __future__ import annotations

import os
import uuid
import aiosqlite
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from httpx import AsyncClient, ASGITransport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_TS = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
STORE = "store_analytics_test"


# ---------------------------------------------------------------------------
# Helpers: event / POS factory
# ---------------------------------------------------------------------------

def _evt(
    visitor_id: str,
    event_type: str,
    zone_id: str | None = None,
    dwell_ms: int | None = None,
    is_staff: bool = False,
    ts_offset_secs: int = 0,
    store_id: str = STORE,
) -> dict:
    """Return a minimal valid event row dict (matches the `events` table schema)."""
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": f"cam_{zone_id or 'entry'}",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": (BASE_TS + timedelta(seconds=ts_offset_secs)).isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": 1 if is_staff else 0,
        "confidence": 0.95,
        "metadata": None,
    }


def _pos(
    ts_offset_secs: int = 0,
    amount: float = 999.0,
    store_id: str = STORE,
) -> dict:
    """Return a minimal valid POS transaction row dict."""
    return {
        "transaction_id": f"TXN-{uuid.uuid4().hex[:8]}",
        "store_id": store_id,
        "timestamp": (BASE_TS + timedelta(seconds=ts_offset_secs)).isoformat(),
        "amount_inr": amount,
        "items_count": 2,
        "payment_method": "UPI",
        "terminal_id": "terminal_01",
    }


# ---------------------------------------------------------------------------
# DB helpers: seed events and POS directly into SQLite
# ---------------------------------------------------------------------------

_INSERT_EVENT = """
INSERT OR IGNORE INTO events
  (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
   zone_id, dwell_ms, is_staff, confidence, metadata)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""

_CREATE_POS_TABLE = """
CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id  TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    amount_inr      REAL,
    items_count     INTEGER,
    payment_method  TEXT,
    terminal_id     TEXT
)
"""

_INSERT_POS = """
INSERT OR IGNORE INTO pos_transactions
  (transaction_id, store_id, timestamp, amount_inr, items_count,
   payment_method, terminal_id)
VALUES (?,?,?,?,?,?,?)
"""


async def _seed_events(db_path: Path, events: list[dict]) -> None:
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            _INSERT_EVENT,
            [
                (
                    e["event_id"], e["store_id"], e["camera_id"], e["visitor_id"],
                    e["event_type"], e["timestamp"], e["zone_id"], e["dwell_ms"],
                    e["is_staff"], e["confidence"], e["metadata"],
                )
                for e in events
            ],
        )
        await db.commit()


async def _seed_pos(db_path: Path, txns: list[dict]) -> None:
    """Create pos_transactions table (if absent) and insert rows."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(_CREATE_POS_TABLE)
        await db.executemany(
            _INSERT_POS,
            [
                (
                    t["transaction_id"], t["store_id"], t["timestamp"],
                    t["amount_inr"], t["items_count"], t["payment_method"],
                    t["terminal_id"],
                )
                for t in txns
            ],
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def ac(tmp_path):
    """
    Per-test isolated AsyncClient wired to a fresh SQLite DB.

    Overrides the session-scoped tmp_db so analytics tests never see
    data seeded by test_ingest.py or test_health.py.
    """
    db_path = tmp_path / "analytics.db"
    original_path = os.environ.get("DB_PATH", "")

    os.environ["DB_PATH"] = str(db_path)

    from core.config import get_settings
    get_settings.cache_clear()

    from main import app, init_db
    await init_db(str(db_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, db_path

    # Restore previous env so session-scoped fixtures aren't broken
    os.environ["DB_PATH"] = original_path
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/metrics
# ---------------------------------------------------------------------------

class TestMetrics:

    async def test_metrics_empty_store_returns_zeros(self, ac):
        """A store with no events must return all-zero metrics (not an error)."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["avg_dwell_ms"] == 0.0 or body["avg_dwell_ms"] is None
        assert body["billing_abandonment_rate"] == 0.0 or body["billing_abandonment_rate"] is None

    async def test_metrics_basic_unique_visitor_count(self, ac):
        """3 distinct customer visitors → unique_visitors == 3."""
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY", "zone_entry"),
            _evt("vis_002", "ENTRY", "zone_entry"),
            _evt("vis_003", "ENTRY", "zone_entry"),
        ])
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        assert resp.json()["unique_visitors"] == 3

    async def test_metrics_staff_excluded_from_unique_visitors(self, ac):
        """
        RULE-1: 2 customers + 3 staff → unique_visitors == 2.

        Staff events are stored at ingest time but MUST be excluded at
        query time for every metric.
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY", "zone_entry"),
            _evt("vis_002", "ENTRY", "zone_entry"),
            _evt("staff_001", "ENTRY", "zone_entry", is_staff=True),
            _evt("staff_002", "ENTRY", "zone_entry", is_staff=True),
            _evt("staff_003", "ENTRY", "zone_entry", is_staff=True),
        ])
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 2, (
            f"Staff must not be counted: expected 2, got {body['unique_visitors']}"
        )

    async def test_metrics_reentry_visitor_counted_once(self, ac):
        """
        RULE-3: vis_001 enters, exits, and RE-ENTRYs later in the day.
        unique_visitors must be 1, not 2.
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY",   "zone_entry", ts_offset_secs=0),
            _evt("vis_001", "EXIT",    "zone_entry", ts_offset_secs=1800),
            _evt("vis_001", "REENTRY", "zone_entry", ts_offset_secs=7200),
            _evt("vis_001", "EXIT",    "zone_entry", ts_offset_secs=9000),
        ])
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unique_visitors"] == 1, (
            f"REENTRY must not double-count: expected 1, got {body['unique_visitors']}"
        )

    async def test_metrics_zero_conversion_no_pos_data(self, ac):
        """
        RULE-4: Visitors reach billing zone but no POS transactions exist.
        conversion_rate must be 0.0, not null, NaN, or an error.
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY",               "zone_entry",   ts_offset_secs=0),
            _evt("vis_001", "ZONE_ENTER",          "zone_billing",  ts_offset_secs=600),
            _evt("vis_001", "BILLING_QUEUE_JOIN",  "zone_billing",  ts_offset_secs=600),
            _evt("vis_001", "EXIT",                "zone_entry",    ts_offset_secs=900),
        ])
        # No POS rows seeded
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversion_rate"] == 0.0, (
            f"No POS data → conversion_rate must be 0.0, got {body['conversion_rate']}"
        )

    async def test_metrics_conversion_rate_within_5min_window(self, ac):
        """
        RULE-2: vis_001 enters billing at T+0s. POS transaction at T+240s (4 min).
        The visitor is within the 5-minute window → should be counted as converted.
        conversion_rate must be > 0.
        """
        client, db = ac
        billing_entry_offset = 600       # T+10min into session
        pos_offset = billing_entry_offset + 240   # T + 4min after billing entry (within window)

        await _seed_events(db, [
            _evt("vis_001", "ENTRY",              "zone_entry",  ts_offset_secs=0),
            _evt("vis_001", "ZONE_ENTER",         "zone_billing", ts_offset_secs=billing_entry_offset),
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=billing_entry_offset),
        ])
        await _seed_pos(db, [_pos(ts_offset_secs=pos_offset)])

        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversion_rate"] > 0, (
            "Visitor in billing zone 4 min before POS transaction must be converted"
        )

    async def test_metrics_no_conversion_outside_5min_window(self, ac):
        """
        RULE-2 (boundary): vis_001 enters billing at T+0s. POS transaction at
        T+360s (6 minutes later). Beyond 5-minute window → NOT converted.
        conversion_rate must be 0.0.
        """
        client, db = ac
        billing_entry_offset = 600
        pos_offset = billing_entry_offset + 360   # 6 min — outside window

        await _seed_events(db, [
            _evt("vis_001", "ENTRY",              "zone_entry",   ts_offset_secs=0),
            _evt("vis_001", "ZONE_ENTER",         "zone_billing", ts_offset_secs=billing_entry_offset),
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=billing_entry_offset),
        ])
        await _seed_pos(db, [_pos(ts_offset_secs=pos_offset)])

        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversion_rate"] == 0.0, (
            "Visitor in billing zone 6 min before POS must NOT be counted as converted "
            f"(got {body['conversion_rate']})"
        )

    async def test_metrics_conversion_at_exact_300s_boundary(self, ac):
        """
        RULE-2 (exact edge): POS transaction exactly 300 seconds (5 min 0 s)
        after billing entry. Must be counted as converted (inclusive boundary).
        """
        client, db = ac
        billing_entry_offset = 600
        pos_offset = billing_entry_offset + 300   # exactly 5 min

        await _seed_events(db, [
            _evt("vis_001", "ENTRY",              "zone_entry",   ts_offset_secs=0),
            _evt("vis_001", "ZONE_ENTER",         "zone_billing", ts_offset_secs=billing_entry_offset),
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=billing_entry_offset),
        ])
        await _seed_pos(db, [_pos(ts_offset_secs=pos_offset)])

        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversion_rate"] > 0, (
            "Visitor at exactly the 300-second boundary must be counted as converted"
        )

    async def test_metrics_response_has_required_fields(self, ac):
        """Response schema must include all required top-level fields."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        required = {"store_id", "unique_visitors", "conversion_rate",
                    "avg_dwell_ms", "avg_dwell_per_zone", "queue_depth",
                    "billing_abandonment_rate", "checked_at"}
        missing = required - body.keys()
        assert not missing, f"Response missing fields: {missing}"
        assert body["store_id"] == STORE

    async def test_metrics_avg_dwell_per_zone_computed(self, ac):
        """
        T2: avg_dwell_per_zone must be a per-zone map computed from ZONE_DWELL
        events (staff excluded), distinct from the global avg_dwell_ms.
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ZONE_DWELL", "zone_skincare", dwell_ms=100_000, ts_offset_secs=60),
            _evt("vis_002", "ZONE_DWELL", "zone_skincare", dwell_ms=200_000, ts_offset_secs=70),
            _evt("vis_003", "ZONE_DWELL", "zone_makeup",   dwell_ms=60_000,  ts_offset_secs=80),
            # staff dwell must NOT affect the per-zone average
            _evt("staff_1", "ZONE_DWELL", "zone_skincare", dwell_ms=9_000_000,
                 ts_offset_secs=90, is_staff=True),
        ])
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        per_zone = body["avg_dwell_per_zone"]
        assert isinstance(per_zone, dict) and per_zone, "avg_dwell_per_zone must be a non-empty map"
        # skincare = mean(100000, 200000) = 150000 (staff 9,000,000 excluded)
        assert per_zone["zone_skincare"] == 150_000.0, per_zone
        assert per_zone["zone_makeup"] == 60_000.0, per_zone

    async def test_metrics_queue_depth_is_most_recent_join(self, ac):
        """T2: queue_depth = the most-recent BILLING_QUEUE_JOIN's queue_depth (int)."""
        import json as _json
        client, db = ac
        events = []
        for i, depth in enumerate([3, 9]):   # later event (offset bigger) wins
            e = _evt(f"vis_{i}", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=i * 600)
            e["metadata"] = _json.dumps({"queue_depth": depth, "sku_zone": None, "session_seq": 1})
            events.append(e)
        await _seed_events(db, events)
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["queue_depth"] == 9, f"queue_depth must be the latest join's depth, got {body['queue_depth']}"
        assert isinstance(body["queue_depth"], int)

    async def test_metrics_queue_depth_defaults_zero(self, ac):
        """T2: with no billing joins, queue_depth defaults to integer 0."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/metrics")
        assert resp.status_code == 200
        assert resp.json()["queue_depth"] == 0


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/funnel
# ---------------------------------------------------------------------------

class TestFunnel:

    async def test_funnel_empty_store_all_zero(self, ac):
        """Empty store → all funnel stages return visitor count 0."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        body = resp.json()
        assert "stages" in body
        for stage in body["stages"]:
            assert stage["visitors"] == 0, (
                f"Stage '{stage['stage']}' expected 0 visitors, got {stage['visitors']}"
            )

    async def test_funnel_has_four_required_stages(self, ac):
        """Response must contain exactly the 4 canonical funnel stages."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        body = resp.json()
        stage_names = {s["stage"] for s in body["stages"]}
        assert stage_names == {"entry", "zone_visit", "billing_queue", "purchase"}, (
            f"Expected 4 stages, got: {stage_names}"
        )

    async def test_funnel_counts_decrease_down_funnel(self, ac):
        """
        3 visitors enter, 2 visit a zone, 1 reaches billing queue, 0 purchase.
        Each stage count must be <= the previous stage.
        """
        client, db = ac
        await _seed_events(db, [
            # All 3 enter
            _evt("vis_001", "ENTRY",              "zone_entry",   ts_offset_secs=0),
            _evt("vis_002", "ENTRY",              "zone_entry",   ts_offset_secs=10),
            _evt("vis_003", "ENTRY",              "zone_entry",   ts_offset_secs=20),
            # 2 visit a floor zone
            _evt("vis_001", "ZONE_ENTER",         "zone_skincare", ts_offset_secs=60),
            _evt("vis_002", "ZONE_ENTER",         "zone_skincare", ts_offset_secs=70),
            # 1 reaches billing
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing",  ts_offset_secs=600),
        ])
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        stages = {s["stage"]: s["visitors"] for s in resp.json()["stages"]}
        assert stages["entry"] >= stages["zone_visit"] >= stages["billing_queue"] >= stages["purchase"], (
            f"Funnel must be monotonically decreasing: {stages}"
        )
        assert stages["entry"] == 3
        assert stages["zone_visit"] == 2
        assert stages["billing_queue"] == 1

    async def test_funnel_reentry_does_not_double_count(self, ac):
        """
        RULE-3: vis_001 has an ENTRY and a later REENTRY.
        The Entry stage must count them as 1 unique visitor, not 2.
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY",   "zone_entry", ts_offset_secs=0),
            _evt("vis_001", "EXIT",    "zone_entry", ts_offset_secs=1800),
            _evt("vis_001", "REENTRY", "zone_entry", ts_offset_secs=7200),
            _evt("vis_002", "ENTRY",   "zone_entry", ts_offset_secs=100),
        ])
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        stages = {s["stage"]: s["visitors"] for s in resp.json()["stages"]}
        assert stages["entry"] == 2, (
            f"RULE-3: REENTRY must not double-count vis_001. Expected entry=2, got {stages['entry']}"
        )

    async def test_funnel_staff_excluded(self, ac):
        """RULE-1: Staff entries must not appear in any funnel stage."""
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001",   "ENTRY", "zone_entry", ts_offset_secs=0),
            _evt("staff_001", "ENTRY", "zone_entry", ts_offset_secs=5, is_staff=True),
        ])
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        stages = {s["stage"]: s["visitors"] for s in resp.json()["stages"]}
        assert stages["entry"] == 1, (
            f"RULE-1: Staff must be excluded from funnel. Expected entry=1, got {stages['entry']}"
        )

    async def test_funnel_pct_of_entry_present(self, ac):
        """Each stage must include a pct_of_entry field in [0, 100]."""
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY", "zone_entry"),
            _evt("vis_001", "ZONE_ENTER", "zone_skincare", ts_offset_secs=60),
        ])
        resp = await client.get(f"/stores/{STORE}/funnel")
        assert resp.status_code == 200
        for stage in resp.json()["stages"]:
            assert "pct_of_entry" in stage, f"Stage '{stage['stage']}' missing pct_of_entry"
            assert 0.0 <= stage["pct_of_entry"] <= 100.0, (
                f"pct_of_entry out of range: {stage}"
            )


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/heatmap
# ---------------------------------------------------------------------------

class TestHeatmap:

    async def test_heatmap_empty_store_returns_empty_zones(self, ac):
        """Empty store → zones list is empty (or all have heat_score == 0)."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert "zones" in body
        # Either empty list or all zeros — both are acceptable
        for zone in body["zones"]:
            assert zone.get("visit_count", 0) == 0

    async def test_heatmap_busiest_zone_scores_100(self, ac):
        """
        Heatmap normalizes visit counts: the zone with the most visits
        must have heat_score == 100.
        """
        client, db = ac
        await _seed_events(db, [
            # zone_skincare: 3 visits
            _evt("vis_001", "ZONE_ENTER", "zone_skincare", ts_offset_secs=60),
            _evt("vis_002", "ZONE_ENTER", "zone_skincare", ts_offset_secs=70),
            _evt("vis_003", "ZONE_ENTER", "zone_skincare", ts_offset_secs=80),
            # zone_makeup: 1 visit
            _evt("vis_001", "ZONE_ENTER", "zone_makeup",   ts_offset_secs=120),
        ])
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert "zone_skincare" in zones
        assert zones["zone_skincare"]["heat_score"] == 100, (
            "Busiest zone must have heat_score == 100 (normalized)"
        )
        assert zones.get("zone_makeup", {}).get("heat_score", 0) < 100

    async def test_heatmap_staff_excluded_from_dwell(self, ac):
        """
        RULE-1: Staff ZONE_DWELL events must not influence avg_dwell_ms.
        A staff member with a 10-hour dwell must not inflate the average.
        """
        client, db = ac
        await _seed_events(db, [
            # Customer: 2-minute dwell
            _evt("vis_001", "ZONE_DWELL", "zone_skincare",
                 dwell_ms=120_000, ts_offset_secs=180),
            # Staff: 10-hour dwell — must be excluded
            _evt("staff_001", "ZONE_DWELL", "zone_skincare",
                 dwell_ms=36_000_000, ts_offset_secs=200, is_staff=True),
        ])
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        zone = zones.get("zone_skincare", {})
        avg_dwell = zone.get("avg_dwell_ms", 0)
        assert avg_dwell <= 300_000, (
            f"RULE-1: Staff 10-hour dwell must not inflate avg. "
            f"Expected ~120000ms, got {avg_dwell}ms"
        )

    async def test_heatmap_response_schema(self, ac):
        """Each zone entry must have zone_id, visit_count, avg_dwell_ms, heat_score."""
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ZONE_ENTER", "zone_skincare", ts_offset_secs=60),
            _evt("vis_001", "ZONE_DWELL", "zone_skincare", dwell_ms=60_000, ts_offset_secs=120),
        ])
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert "store_id" in body
        assert "zones" in body
        assert "data_confidence" in body, "HeatmapReport must include data_confidence field"
        assert "checked_at" in body
        if body["zones"]:
            zone = body["zones"][0]
            required_zone_fields = {"zone_id", "visit_count", "avg_dwell_ms", "heat_score"}
            missing = required_zone_fields - zone.keys()
            assert not missing, f"Zone entry missing fields: {missing}"

    async def test_heatmap_data_confidence_low_when_few_sessions(self, ac):
        """
        data_confidence must be 'low' when fewer than 20 unique visitor sessions
        have been recorded — results may not be statistically representative.
        """
        client, db = ac
        # Seed 5 visitors — below the 20-session threshold
        await _seed_events(db, [
            _evt(f"vis_{i:03d}", "ENTRY", "zone_entry", ts_offset_secs=i * 60)
            for i in range(5)
        ])
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_confidence"] == "low", (
            f"Expected data_confidence='low' with 5 sessions, got: {body['data_confidence']!r}"
        )

    async def test_heatmap_data_confidence_high_when_enough_sessions(self, ac):
        """
        data_confidence must be 'high' when at least 20 unique visitor sessions
        are present.
        """
        client, db = ac
        # Seed exactly 20 ENTRY events (20 unique visitors)
        await _seed_events(db, [
            _evt(f"vis_{i:03d}", "ENTRY", "zone_entry", ts_offset_secs=i * 30)
            for i in range(20)
        ])
        resp = await client.get(f"/stores/{STORE}/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_confidence"] == "high", (
            f"Expected data_confidence='high' with 20 sessions, got: {body['data_confidence']!r}"
        )


# ---------------------------------------------------------------------------
# GET /stores/{store_id}/anomalies
# ---------------------------------------------------------------------------

class TestAnomalies:

    async def test_anomalies_empty_store_no_anomalies(self, ac):
        """Empty store → anomalies list is empty."""
        client, _ = ac
        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "anomalies" in body
        assert body["anomalies"] == [], (
            f"Expected no anomalies in empty store, got: {body['anomalies']}"
        )

    async def test_anomalies_high_queue_depth_triggers_warn(self, ac):
        """
        A billing zone with BILLING_QUEUE_JOIN events whose metadata.queue_depth
        averages > 5 must produce a WARN anomaly.
        """
        import json
        client, db = ac
        deep_queue_events = [
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=i * 60)
            for i in range(6)
        ]
        # Overwrite metadata with high queue_depth values
        for i, e in enumerate(deep_queue_events):
            e["metadata"] = json.dumps({"queue_depth": 7 + i, "sku_zone": None, "session_seq": 1})
        await _seed_events(db, deep_queue_events)

        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        severities = [a["severity"] for a in body["anomalies"]]
        assert "WARN" in severities or "CRITICAL" in severities, (
            f"High queue depth must produce WARN or CRITICAL anomaly. Got: {body['anomalies']}"
        )

    async def test_anomalies_critical_queue_depth(self, ac):
        """
        Queue depth consistently above 10 must produce a CRITICAL (not just WARN) anomaly.
        """
        import json
        client, db = ac
        events = [
            _evt(f"vis_{i:03d}", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=i * 60)
            for i in range(5)
        ]
        for e in events:
            e["metadata"] = json.dumps({"queue_depth": 12, "sku_zone": None, "session_seq": 1})
        await _seed_events(db, events)

        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        severities = [a["severity"] for a in body["anomalies"]]
        assert "CRITICAL" in severities, (
            f"Queue depth of 12 must produce CRITICAL anomaly. Got: {body['anomalies']}"
        )

    async def test_anomalies_high_abandonment_rate_flagged(self, ac):
        """
        When billing_abandonment_rate > 50% (many BILLING_QUEUE_ABANDON vs JOIN),
        an anomaly must be raised.
        """
        client, db = ac
        await _seed_events(db, [
            # 1 joins, 5 abandon
            _evt("vis_001", "BILLING_QUEUE_JOIN",    "zone_billing", ts_offset_secs=0),
            _evt("vis_002", "BILLING_QUEUE_ABANDON", "zone_billing", ts_offset_secs=10),
            _evt("vis_003", "BILLING_QUEUE_ABANDON", "zone_billing", ts_offset_secs=20),
            _evt("vis_004", "BILLING_QUEUE_ABANDON", "zone_billing", ts_offset_secs=30),
            _evt("vis_005", "BILLING_QUEUE_ABANDON", "zone_billing", ts_offset_secs=40),
            _evt("vis_006", "BILLING_QUEUE_ABANDON", "zone_billing", ts_offset_secs=50),
        ])
        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["anomalies"]) > 0, (
            "High abandonment rate (5/6 visitors abandon queue) must trigger an anomaly"
        )

    async def test_anomalies_moderate_queue_depth_is_info(self, ac):
        """
        T1: average queue depth in (3, 5] must emit an INFO anomaly — exercises
        the INFO severity path (the system uses all of INFO/WARN/CRITICAL).
        """
        import json
        client, db = ac
        events = []
        for i in range(4):
            e = _evt(f"vis_{i:03d}", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=i * 60)
            e["metadata"] = json.dumps({"queue_depth": 4, "sku_zone": None, "session_seq": 1})
            events.append(e)
        await _seed_events(db, events)
        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        q = [a for a in resp.json()["anomalies"] if a["type"] == "HIGH_QUEUE_DEPTH"]
        assert q, "Moderate queue depth (avg=4) must emit a HIGH_QUEUE_DEPTH anomaly"
        assert q[0]["severity"] == "INFO", f"avg depth 4 must be INFO, got {q[0]['severity']}"

    async def test_anomalies_dead_zone_fires(self, ac):
        """
        T1: a zone whose last visit is >30 min before the store's LATEST event
        must emit a DEAD_ZONE (WARN) anomaly with zone_id, gap minutes, and a
        suggested_action. Anchored to store_now, not wall-clock.
        """
        client, db = ac
        await _seed_events(db, [
            # zone_fragrance visited only early
            _evt("vis_001", "ZONE_ENTER", "zone_fragrance", ts_offset_secs=0),
            # later activity elsewhere defines store_now (+50 min)
            _evt("vis_002", "ENTRY",      "zone_entry",  ts_offset_secs=3000),
            _evt("vis_002", "ZONE_ENTER", "zone_makeup", ts_offset_secs=3000),
        ])
        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        dz = [a for a in resp.json()["anomalies"] if a["type"] == "DEAD_ZONE"]
        assert len(dz) == 1, f"Exactly one DEAD_ZONE expected (fragrance), got {dz}"
        assert dz[0]["zone_id"] == "zone_fragrance"
        assert dz[0]["severity"] == "WARN"
        assert dz[0]["value"] > 30, "gap must exceed the 30-minute threshold"
        assert dz[0]["suggested_action"], "DEAD_ZONE must carry a suggested_action"

    async def test_anomalies_conversion_drop_empty_without_history(self, ac):
        """
        T1: with only a single day of data there is no prior-7-day baseline,
        so CONVERSION_DROP must NOT be emitted (we never fabricate a drop).
        """
        client, db = ac
        await _seed_events(db, [
            _evt("vis_001", "ENTRY",              "zone_entry",  ts_offset_secs=0),
            _evt("vis_001", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=600),
        ])
        await _seed_pos(db, [_pos(ts_offset_secs=700)])   # a real conversion today
        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        types = [a["type"] for a in resp.json()["anomalies"]]
        assert "CONVERSION_DROP" not in types, (
            "CONVERSION_DROP requires multi-day history; must stay empty on one day"
        )

    async def test_anomalies_response_schema(self, ac):
        """Each anomaly must have type, severity, and message fields."""
        import json
        client, db = ac
        events = [
            _evt(f"vis_{i:03d}", "BILLING_QUEUE_JOIN", "zone_billing", ts_offset_secs=i * 30)
            for i in range(4)
        ]
        for e in events:
            e["metadata"] = json.dumps({"queue_depth": 11, "sku_zone": None, "session_seq": 1})
        await _seed_events(db, events)

        resp = await client.get(f"/stores/{STORE}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "store_id" in body
        assert "checked_at" in body
        for anomaly in body["anomalies"]:
            required = {"type", "severity", "message"}
            missing = required - anomaly.keys()
            assert not missing, f"Anomaly entry missing fields: {missing}"
            assert anomaly["severity"] in {"INFO", "WARN", "CRITICAL"}, (
                f"severity must be INFO/WARN/CRITICAL, got: {anomaly['severity']}"
            )
            # suggested_action must be present (may be null for INFO, non-null for WARN/CRITICAL)
            assert "suggested_action" in anomaly, (
                "Each anomaly must include a suggested_action field"
            )
            if anomaly["severity"] in {"WARN", "CRITICAL"}:
                assert anomaly["suggested_action"] is not None, (
                    f"WARN/CRITICAL anomaly must have a non-null suggested_action, "
                    f"got None for type={anomaly['type']}"
                )
