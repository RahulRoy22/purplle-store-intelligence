# PROMPT: Write pytest-asyncio tests for a FastAPI /health endpoint that returns
#   {status, db, stale_feed, last_event_at, checked_at}. Cover: (1) happy path
#   with empty DB — status=ok, db=connected, stale_feed=False; (2) degraded when
#   DB path is unwritable — db=error, status=degraded; (3) stale_feed=True when
#   last event is older than STALE_FEED_THRESHOLD_MINUTES (default 10 min).
#
# CHANGES MADE: Added timezone-aware datetime comparison (UTC) for stale_feed
#   calculation so tests are deterministic regardless of system clock timezone.
#   Used aiosqlite directly in the stale-feed fixture rather than going through
#   the API to seed the exact timestamp needed for the 15-minute-old event.
"""
Tests for GET /health endpoint.
"""
import pytest
import os
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_ok_empty_db(client: AsyncClient):
    """With no events, DB is connected, stale_feed is False, status is ok."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert body["stale_feed"] is False
    assert body["last_event_at"] is None
    assert "checked_at" in body


@pytest.mark.asyncio
async def test_health_degraded_missing_db(tmp_db):
    """If DB path points to an unwritable location, db == 'error'."""
    from core.config import get_settings

    original_db_path = os.environ.get("DB_PATH", "")
    os.environ["DB_PATH"] = "/nonexistent_dir/bad.db"
    get_settings.cache_clear()

    from main import app
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/health")

    body = resp.json()
    assert body["db"] == "error"
    assert body["status"] == "degraded"

    # Restore so subsequent tests use the correct shared temp DB
    os.environ["DB_PATH"] = original_db_path
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_stale_feed(client: AsyncClient, tmp_db):
    """Insert an event older than 10 min — stale_feed must be True."""
    import aiosqlite
    from datetime import datetime, timezone, timedelta

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    async with aiosqlite.connect(str(tmp_db)) as db:
        await db.execute(
            """INSERT INTO events
               (event_id, store_id, camera_id, visitor_id, event_type,
                timestamp, is_staff, confidence, metadata)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "evt-stale-001", "store_001", "cam_entry", "vis_001",
                "ENTRY", old_ts, 0, 0.95, "{}",
            ),
        )
        await db.commit()

    resp = await client.get("/health")
    body = resp.json()
    assert body["stale_feed"] is True
    assert body["status"] == "degraded"
    assert body["last_event_at"] is not None
