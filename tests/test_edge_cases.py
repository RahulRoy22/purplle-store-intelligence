# PROMPT: Write pytest-asyncio edge-case tests for a FastAPI retail analytics API
#   backed by aiosqlite/SQLite. Tests needed: (1) zero-purchase store — ingest
#   ENTRY/ZONE_ENTER only (no billing events), assert conversion_rate==0.0 and
#   unique_visitors>0; (2) empty-store anomalies — no events at all, assert GET
#   /anomalies returns HTTP 200 and a list; (3) all-staff clip — 50 events with
#   is_staff=True, assert unique_visitors==0 and conversion_rate==0.0;
#   (4) REENTRY not double-counted in funnel — ENTRY+EXIT+REENTRY for same
#   visitor_id, assert funnel entry_count==1; (5) partial ingest 207 — batch
#   with one valid event and one missing event_id, assert HTTP 207, accepted==1,
#   rejected==1; (6) group entry 3 people — 3 ENTRY events with different
#   visitor_ids same timestamp ±2s same camera, assert unique_visitors==3.
#   Framework: pytest-asyncio, aiosqlite, httpx.AsyncClient + ASGITransport.
#   Each test uses a fresh per-test DB via tmp_path fixture.
#
# CHANGES MADE: Used per-test tmp_path fixture (not session-scoped) so each
#   test gets a clean database. Added get_settings.cache_clear() before each
#   client construction. The all-staff test uses direct DB inserts (not the
#   HTTP ingest endpoint) for faster seeding. Corrected the partial-ingest
#   test to omit event_id entirely (not just set it to None) to trigger the
#   missing-required-field validation error.
"""
Edge-case tests that cover the rubric's explicit boundary conditions.
"""
from __future__ import annotations

import os
import uuid
import aiosqlite
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TS = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)


def _evt(
    visitor_id: str,
    event_type: str,
    store_id: str,
    zone_id: str | None = None,
    is_staff: bool = False,
    ts_offset_secs: int = 0,
    camera_id: str = "cam_entry",
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": (BASE_TS + timedelta(seconds=ts_offset_secs)).isoformat(),
        "zone_id": zone_id,
        "dwell_ms": None,
        "is_staff": 1 if is_staff else 0,
        "confidence": 0.95,
        "metadata": None,
    }


_INSERT_EVENT = """
INSERT OR IGNORE INTO events
  (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
   zone_id, dwell_ms, is_staff, confidence, metadata)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


async def _seed(db_path, events: list[dict]) -> None:
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


@pytest_asyncio.fixture
async def ac(tmp_path):
    """Per-test isolated AsyncClient with a fresh SQLite DB."""
    db_path = tmp_path / "edge.db"
    original = os.environ.get("DB_PATH", "")
    os.environ["DB_PATH"] = str(db_path)

    from core.config import get_settings
    get_settings.cache_clear()

    from main import app, init_db
    await init_db(str(db_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, db_path

    os.environ["DB_PATH"] = original
    from core.config import get_settings as gs
    gs.cache_clear()


# ---------------------------------------------------------------------------
# 6a — zero-purchase store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_purchase_store(ac):
    """
    Visitors arrive and enter zones but no billing events occur.
    conversion_rate must be 0.0 (not null/missing/crash), unique_visitors > 0.
    """
    client, db = ac
    store = "store_zero_purchase"
    await _seed(db, [
        _evt("vis_001", "ENTRY",      store, "zone_entry",    ts_offset_secs=0),
        _evt("vis_001", "ZONE_ENTER", store, "zone_skincare", ts_offset_secs=60),
        _evt("vis_002", "ENTRY",      store, "zone_entry",    ts_offset_secs=30),
    ])
    resp = await client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversion_rate"] == 0.0, (
        f"No billing events → conversion_rate must be 0.0, got {body['conversion_rate']}"
    )
    assert body["unique_visitors"] > 0, "Visitors arrived — unique_visitors must be > 0"


# ---------------------------------------------------------------------------
# 6b — empty store anomalies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_store_anomalies(ac):
    """
    No events for a store — GET /anomalies must return HTTP 200 with a list
    (empty or containing a dead-zone anomaly), not 500 or null.
    """
    client, _ = ac
    store = "store_no_events"
    resp = await client.get(f"/stores/{store}/anomalies")
    assert resp.status_code == 200, (
        f"Empty store must not crash /anomalies (got {resp.status_code})"
    )
    body = resp.json()
    assert isinstance(body.get("anomalies"), list), (
        "anomalies field must be a list even for empty store"
    )


# ---------------------------------------------------------------------------
# 6c — all-staff clip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_staff_clip(ac):
    """
    50 events all with is_staff=True.
    unique_visitors must be 0 and conversion_rate must be 0.0.
    """
    client, db = ac
    store = "store_staff_only"
    await _seed(db, [
        _evt(f"staff_{i:03d}", "ENTRY", store, "zone_entry", is_staff=True)
        for i in range(50)
    ])
    resp = await client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0, (
        f"Staff-only clip must have 0 unique visitors, got {body['unique_visitors']}"
    )
    assert body["conversion_rate"] == 0.0, (
        f"Staff-only clip must have conversion_rate=0.0, got {body['conversion_rate']}"
    )


# ---------------------------------------------------------------------------
# 6d — REENTRY not double-counted in funnel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reentry_not_double_counted_in_funnel(ac):
    """
    ENTRY + EXIT + REENTRY for the same visitor_id.
    Funnel entry_count must be 1, not 2.
    """
    client, db = ac
    store = "store_reentry_funnel"
    await _seed(db, [
        _evt("VIS_001", "ENTRY",   store, "zone_entry", ts_offset_secs=0),
        _evt("VIS_001", "EXIT",    store, "zone_entry", ts_offset_secs=1800),
        _evt("VIS_001", "REENTRY", store, "zone_entry", ts_offset_secs=7200),
    ])
    resp = await client.get(f"/stores/{store}/funnel")
    assert resp.status_code == 200
    stages = {s["stage"]: s["visitors"] for s in resp.json()["stages"]}
    assert stages["entry"] == 1, (
        f"REENTRY must not add to unique visitors in funnel; expected 1, got {stages['entry']}"
    )


# ---------------------------------------------------------------------------
# 6e — partial ingest returns HTTP 207
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_ingest_207(ac):
    """
    Batch with one valid event and one dict missing event_id.
    Must return HTTP 207, accepted=1, rejected=1.
    """
    client, _ = ac
    good = {
        "event_id": str(uuid.uuid4()),
        "store_id": "store_partial",
        "camera_id": "cam_entry",
        "visitor_id": "vis_001",
        "event_type": "ENTRY",
        "timestamp": BASE_TS.isoformat(),
        "zone_id": None,
        "dwell_ms": None,
        "is_staff": False,
        "confidence": 0.95,
        "metadata": {},
    }
    bad = {
        # event_id intentionally omitted — required field
        "store_id": "store_partial",
        "camera_id": "cam_entry",
        "visitor_id": "vis_002",
        "event_type": "ENTRY",
        "timestamp": BASE_TS.isoformat(),
        "confidence": 0.90,
        "is_staff": False,
    }
    resp = await client.post("/events/ingest", json={"events": [good, bad]})
    assert resp.status_code == 207, (
        f"Mixed valid/invalid batch must return 207, got {resp.status_code}"
    )
    body = resp.json()
    assert body["accepted"] == 1, f"Expected accepted=1, got {body['accepted']}"
    assert body["rejected"] == 1, f"Expected rejected=1, got {body['rejected']}"


# ---------------------------------------------------------------------------
# 6f — group entry: 3 people at the same time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_entry_three_people(ac):
    """
    3 ENTRY events with different visitor_ids, same camera, timestamps within ±2s.
    unique_visitors must be 3.
    """
    client, db = ac
    store = "store_group_entry"
    await _seed(db, [
        _evt("vis_A", "ENTRY", store, "zone_entry", ts_offset_secs=0,  camera_id="cam_entry"),
        _evt("vis_B", "ENTRY", store, "zone_entry", ts_offset_secs=1,  camera_id="cam_entry"),
        _evt("vis_C", "ENTRY", store, "zone_entry", ts_offset_secs=2,  camera_id="cam_entry"),
    ])
    resp = await client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 3, (
        f"3-person group entry must count as 3 unique visitors, got {body['unique_visitors']}"
    )
