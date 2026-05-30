"""
Phase 3 Tests — Analytics / Intelligence API  (RED phase)

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

Prompt used to scaffold adversarial data patterns (AI-assisted):
  "Design pytest fixtures that seed SQLite with events that stress-test:
   (a) staff-only stores, (b) re-entry visitors, (c) POS transactions at the
   exact edge of the 5-minute window, (d) billing abandonment with no purchase."
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
                    "avg_dwell_ms", "billing_abandonment_rate", "checked_at"}
        missing = required - body.keys()
        assert not missing, f"Response missing fields: {missing}"
        assert body["store_id"] == STORE


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
        assert "checked_at" in body
        if body["zones"]:
            zone = body["zones"][0]
            required_zone_fields = {"zone_id", "visit_count", "avg_dwell_ms", "heat_score"}
            missing = required_zone_fields - zone.keys()
            assert not missing, f"Zone entry missing fields: {missing}"


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
