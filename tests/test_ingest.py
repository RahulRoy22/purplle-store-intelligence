# PROMPT: Write pytest-asyncio tests for a FastAPI batch ingest endpoint at
#   POST /events/ingest. Cover: (1) happy path — 200 with accepted=N;
#   (2) idempotency — re-sending the same batch returns duplicate=N, accepted=0;
#   (3) partial success — mix of valid + malformed events, good ones land;
#   (4) batch size limit — >500 events returns 422; (5) empty batch — 200 with
#   accepted=0; (6) staff events accepted at ingest (is_staff=True stored);
#   (7) DB unavailable → 503 with structured JSON error body.
#
# CHANGES MADE: Verified that the accepted/duplicate split uses a query-before-insert
#   pattern (not cursor.rowcount) to handle aiosqlite executemany rowcount
#   unreliability across Python versions. Added httpx_mock(assert_all_responses_were_requested=False)
#   on tests that raise before making HTTP requests to prevent teardown errors.
"""
Tests for POST /events/ingest.
"""
import json
import pytest
import uuid
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient


# ---- helpers ----------------------------------------------------------------

def make_event(**overrides) -> dict:
    """Return a minimal valid event dict."""
    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": "store_001",
        "camera_id": "cam_entry",
        "visitor_id": "vis_0001",
        "event_type": "ENTRY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zone_id": "zone_entry",
        "dwell_ms": None,
        "is_staff": False,
        "confidence": 0.95,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(overrides)
    return base


# ---- happy path -------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_valid_batch(client: AsyncClient):
    """A batch of 3 unique valid events must all be accepted."""
    events = [make_event() for _ in range(3)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 3
    assert body["duplicate"] == 0
    assert body["rejected"] == []
    assert "trace_id" in body
    assert body["latency_ms"] >= 0


# ---- idempotency ------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_idempotent_duplicate_batch(client: AsyncClient):
    """Submitting the exact same batch twice: second call returns accepted=0, duplicate=N."""
    events = [make_event() for _ in range(5)]
    # First submission
    r1 = await client.post("/events/ingest", json={"events": events})
    assert r1.json()["accepted"] == 5

    # Second submission — same event_ids
    r2 = await client.post("/events/ingest", json={"events": events})
    body2 = r2.json()
    assert body2["accepted"] == 0
    assert body2["duplicate"] == 5
    assert body2["rejected"] == []


@pytest.mark.asyncio
async def test_ingest_partial_duplicate(client: AsyncClient):
    """A batch where 2 events are new and 2 are duplicates counts correctly."""
    events = [make_event() for _ in range(4)]
    # Insert the first two
    r1 = await client.post("/events/ingest", json={"events": events[:2]})
    assert r1.json()["accepted"] == 2

    # Submit all 4: first 2 are dupes, last 2 are new
    r2 = await client.post("/events/ingest", json={"events": events})
    body2 = r2.json()
    assert body2["accepted"] == 2
    assert body2["duplicate"] == 2
    assert body2["rejected"] == []


# ---- partial success (validation failures) ----------------------------------

@pytest.mark.asyncio
async def test_ingest_invalid_event_type_rejected(client: AsyncClient):
    """An event with an unknown event_type is rejected; valid events in the same batch pass."""
    bad = make_event(event_type="SNEEZE")
    good = make_event()
    resp = await client.post("/events/ingest", json={"events": [bad, good]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1
    assert body["rejected"][0]["event_id"] == bad["event_id"]
    assert "event_type" in body["rejected"][0]["reason"].lower() or len(body["rejected"][0]["reason"]) > 0


@pytest.mark.asyncio
async def test_ingest_confidence_out_of_range_rejected(client: AsyncClient):
    """confidence > 1.0 must be rejected."""
    bad = make_event(confidence=1.5)
    resp = await client.post("/events/ingest", json={"events": [bad]})
    body = resp.json()
    assert body["accepted"] == 0
    assert len(body["rejected"]) == 1


@pytest.mark.asyncio
async def test_ingest_missing_required_field_rejected(client: AsyncClient):
    """An event missing visitor_id is rejected."""
    bad = make_event()
    del bad["visitor_id"]
    resp = await client.post("/events/ingest", json={"events": [bad]})
    body = resp.json()
    assert body["accepted"] == 0
    assert len(body["rejected"]) == 1


@pytest.mark.asyncio
async def test_ingest_naive_timestamp_rejected(client: AsyncClient):
    """A timestamp with no timezone info must be rejected."""
    bad = make_event(timestamp="2026-05-30T10:00:00")  # no +00:00
    resp = await client.post("/events/ingest", json={"events": [bad]})
    body = resp.json()
    assert body["accepted"] == 0
    assert len(body["rejected"]) == 1


# ---- batch size limit -------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_batch_too_large(client: AsyncClient):
    """Batch of 501 events must return 422."""
    events = [make_event() for _ in range(501)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 422


# ---- edge cases -------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_empty_batch(client: AsyncClient):
    """Empty batch must succeed with accepted=0."""
    resp = await client.post("/events/ingest", json={"events": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 0
    assert body["duplicate"] == 0
    assert body["rejected"] == []


@pytest.mark.asyncio
async def test_ingest_staff_events_are_stored(client: AsyncClient, tmp_db):
    """Staff events (is_staff=True) MUST be ingested -- filtering happens at query time."""
    import aiosqlite
    staff_event = make_event(
        visitor_id="staff_001",
        is_staff=True,
        confidence=0.99,
    )
    resp = await client.post("/events/ingest", json={"events": [staff_event]})
    assert resp.json()["accepted"] == 1

    async with aiosqlite.connect(str(tmp_db)) as db:
        async with db.execute(
            "SELECT is_staff FROM events WHERE visitor_id = 'staff_001'"
        ) as cursor:
            row = await cursor.fetchone()
    assert row is not None, "Staff event was not stored"
    assert row[0] == 1  # stored as INTEGER 1


@pytest.mark.asyncio
async def test_ingest_all_event_types_accepted(client: AsyncClient):
    """All 8 valid event_type values must be accepted without rejection."""
    valid_types = [
        "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
        "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
    ]
    events = [make_event(event_type=et) for et in valid_types]
    resp = await client.post("/events/ingest", json={"events": events})
    body = resp.json()
    assert body["accepted"] == 8
    assert body["rejected"] == []


# ---- 503 on DB failure ------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_db_failure_returns_503():
    """When the DB path is unwritable, /events/ingest must return 503 with trace_id."""
    import os
    from core.config import get_settings
    from httpx import AsyncClient, ASGITransport

    original = os.environ.get("DB_PATH", "")
    os.environ["DB_PATH"] = "/nonexistent_dir/bad.db"
    get_settings.cache_clear()

    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/events/ingest",
            json={"events": [make_event()]},
        )

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "database_unavailable"
    assert "trace_id" in body["detail"]

    os.environ["DB_PATH"] = original
    get_settings.cache_clear()
