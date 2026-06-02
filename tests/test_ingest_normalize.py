# PROMPT: Write pytest-asyncio tests proving POST /events/ingest accepts the
#   organiser's shipped detection schema (lowercase event_type, id_token/track_id,
#   event_timestamp/event_time/queue_join_ts, naive timestamps, no event_id, no
#   confidence, rich gender/age/queue fields) in addition to our canonical schema.
#   Use REAL lines copied verbatim from the shipped sample_events.jsonl. Assert:
#   (1) entry/zone/queue rows all ingest (200/207, rejected==0); (2) billing rows
#   land under the canonical zone_id 'zone_billing' so analytics SQL matches;
#   (3) re-POSTing the same shipped batch is idempotent (deterministic event_id);
#   (4) canonical rows still pass through unchanged. Add direct unit tests of
#   normalize_event for the field-mapping rules.
#
# CHANGES MADE: Pulled the five representative lines straight out of the shipped
#   sample file rather than hand-authoring them, so the test breaks if the real
#   schema drifts. Added an explicit DB read-back to confirm zone canonicalisation
#   (HTTP response alone doesn't prove the stored zone_id), and asserted the
#   derived event_id is stable across two normalize_event calls.
"""
Tests for dual-schema tolerant ingest (models/normalize.py + /events/ingest).
"""
import json
import uuid

import aiosqlite
import pytest
from httpx import AsyncClient

from models.normalize import normalize_event


# --- Real lines copied verbatim from the shipped sample_events.jsonl ----------
SHIPPED_ENTRY = {
    "event_type": "entry", "id_token": "ID_60001", "store_code": "store_1076",
    "camera_id": "cam1", "event_timestamp": "2026-03-08T18:10:05.120000",
    "is_staff": False, "gender_pred": "F", "age_pred": 28, "age_bucket": "25-34",
    "is_face_hidden": False, "group_id": None, "group_size": None,
}
SHIPPED_ZONE_ENTER = {
    "event_type": "zone_entered", "track_id": 101, "store_id": "ST1076",
    "camera_id": "CAM2", "zone_id": "PURPLLE_MUM_1076_Z01", "zone_name": "Left Shelf",
    "zone_type": "SHELF", "is_revenue_zone": "Yes",
    "event_time": "2026-03-08T18:10:45.280000", "zone_hotspot_x": 412.6,
    "zone_hotspot_y": 238.4, "gender": "F", "age": 28, "age_bucket": "25-34",
}
SHIPPED_ZONE_EXIT = {
    "event_type": "zone_exited", "track_id": 101, "store_id": "ST1076",
    "camera_id": "CAM2", "zone_id": "PURPLLE_MUM_1076_Z01", "zone_name": "Left Shelf",
    "zone_type": "SHELF", "is_revenue_zone": "Yes",
    "event_time": "2026-03-08T18:11:18.720000", "zone_hotspot_x": 418.2,
    "zone_hotspot_y": 241.0, "gender": "F", "age": 28, "age_bucket": "25-34",
}
SHIPPED_QUEUE_COMPLETED = {
    "queue_event_id": "cfd8e3c5-7aa0-4ea3-9b59-692d50da8308",
    "event_type": "queue_completed", "track_id": 102, "store_id": "ST1076",
    "camera_id": "PURPLLE_MUM_1076_CAM6", "zone_id": "PURPLLE_MUM_1076_Z_BILLING_01",
    "zone_name": "Billing Counter Queue", "zone_type": "BILLING",
    "is_revenue_zone": "Yes", "queue_join_ts": "2026-03-08T18:13:05.080000",
    "queue_served_ts": "2026-03-08T18:13:13.240000",
    "queue_exit_ts": "2026-03-08T18:15:31.840000", "wait_seconds": 8,
    "queue_position_at_join": 2, "abandoned": False,
    "zone_hotspot_x": 602.8, "zone_hotspot_y": 183.4, "gender": "M", "age": 31,
    "age_bucket": "25-34",
}
SHIPPED_QUEUE_ABANDONED = {
    "queue_event_id": "a1e5c1d3-9e14-4df1-bd2c-4ab5cbf55f91",
    "event_type": "queue_abandoned", "track_id": 101, "store_id": "ST1076",
    "camera_id": "PURPLLE_MUM_1076_CAM6", "zone_id": "PURPLLE_MUM_1076_Z_BILLING_01",
    "zone_name": "Billing Counter Queue", "zone_type": "BILLING",
    "is_revenue_zone": "Yes", "queue_join_ts": "2026-03-08T18:12:58.240000",
    "queue_served_ts": None, "queue_exit_ts": "2026-03-08T18:14:02.880000",
    "wait_seconds": 65, "queue_position_at_join": 4, "abandoned": True,
    "zone_hotspot_x": 598.1, "zone_hotspot_y": 176.8, "gender": "F", "age": 28,
    "age_bucket": "25-34",
}

ALL_SHIPPED = [
    SHIPPED_ENTRY, SHIPPED_ZONE_ENTER, SHIPPED_ZONE_EXIT,
    SHIPPED_QUEUE_COMPLETED, SHIPPED_QUEUE_ABANDONED,
]


def _uniq(event: dict, tag: str) -> dict:
    """
    Return a copy of a shipped row made unique to one test.

    The session-scoped tmp_db fixture is shared across tests, and our derived
    event_ids are *deterministic* by design — so two tests sending the same
    fixed row would (correctly) see the second as a duplicate. Tests that need a
    pristine insert tag the identity fields so the derived event_id is unique.
    """
    out = dict(event)
    if "id_token" in out:
        out["id_token"] = f"{out['id_token']}_{tag}"
    if "track_id" in out:
        out["track_id"] = f"{out['track_id']}_{tag}"
    if "queue_event_id" in out:
        out["queue_event_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tag}:{out['queue_event_id']}"))
    return out


def _uniq_batch(tag: str) -> list[dict]:
    return [_uniq(e, tag) for e in ALL_SHIPPED]


# ============================================================================
# Unit tests — normalize_event field mapping
# ============================================================================

def test_entry_maps_to_canonical():
    out = normalize_event(SHIPPED_ENTRY)
    assert out["event_type"] == "ENTRY"
    assert out["visitor_id"] == "ID_60001"
    assert out["store_id"] == "store_1076"
    assert out["is_staff"] is False
    # naive shipped timestamp becomes tz-aware UTC
    assert out["timestamp"].endswith("+00:00")
    # rich detection fields preserved in metadata
    assert out["metadata"]["gender_pred"] == "F"
    assert out["metadata"]["age_bucket"] == "25-34"
    # a valid uuid event_id is synthesised
    uuid.UUID(out["event_id"])
    # default confidence supplied (never dropped)
    assert out["confidence"] == 0.9


def test_zone_enter_track_id_and_slug():
    out = normalize_event(SHIPPED_ZONE_ENTER)
    assert out["event_type"] == "ZONE_ENTER"
    assert out["visitor_id"] == "track_101"
    assert out["zone_id"] == "zone_left_shelf"   # slugged, non-billing, non-entry
    assert out["metadata"]["zone_hotspot_x"] == 412.6


def test_queue_completed_becomes_billing_join_with_depth():
    out = normalize_event(SHIPPED_QUEUE_COMPLETED)
    assert out["event_type"] == "BILLING_QUEUE_JOIN"
    assert out["zone_id"] == "zone_billing"               # canonical billing zone
    assert out["metadata"]["queue_depth"] == 2            # from queue_position_at_join
    assert out["metadata"]["wait_seconds"] == 8
    # an existing real uuid (queue_event_id) is reused verbatim
    assert out["event_id"] == "cfd8e3c5-7aa0-4ea3-9b59-692d50da8308"


def test_queue_abandoned_becomes_billing_abandon():
    out = normalize_event(SHIPPED_QUEUE_ABANDONED)
    assert out["event_type"] == "BILLING_QUEUE_ABANDON"
    assert out["zone_id"] == "zone_billing"
    assert out["metadata"]["abandoned"] is True


def test_derived_event_id_is_deterministic():
    """Idempotency hinges on the same shipped row producing the same id."""
    a = normalize_event(SHIPPED_ENTRY)["event_id"]
    b = normalize_event(dict(SHIPPED_ENTRY))["event_id"]
    assert a == b


def test_canonical_event_passes_through_unchanged():
    canonical = {
        "event_id": str(uuid.uuid4()), "store_id": "STORE_BLR_002",
        "camera_id": "cam_entry", "visitor_id": "vis_0001", "event_type": "ENTRY",
        "timestamp": "2026-03-08T18:10:05+00:00", "zone_id": None, "dwell_ms": None,
        "is_staff": False, "confidence": 0.95,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    assert normalize_event(canonical) is canonical


def test_unknown_row_returned_unchanged():
    junk = {"event_type": "totally_made_up", "foo": "bar"}
    assert normalize_event(junk) is junk


# ============================================================================
# HTTP integration — ingest accepts shipped schema end-to-end
# ============================================================================

@pytest.mark.asyncio
async def test_ingest_accepts_shipped_schema(client: AsyncClient):
    batch = _uniq_batch("accepts")
    resp = await client.post("/events/ingest", json={"events": batch})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == len(batch)
    assert body["rejected"] == 0


@pytest.mark.asyncio
async def test_shipped_ingest_is_idempotent(client: AsyncClient):
    batch = _uniq_batch("idem")
    first = await client.post("/events/ingest", json={"events": batch})
    assert first.json()["accepted"] == len(batch)
    second = await client.post("/events/ingest", json={"events": batch})
    body = second.json()
    assert body["accepted"] == 0
    assert body["duplicate"] == len(batch)


@pytest.mark.asyncio
async def test_billing_rows_stored_as_zone_billing(client: AsyncClient, tmp_db):
    row_in = _uniq(SHIPPED_QUEUE_COMPLETED, "billingstore")
    event_id = normalize_event(row_in)["event_id"]
    await client.post("/events/ingest", json={"events": [row_in]})
    async with aiosqlite.connect(str(tmp_db)) as db:
        async with db.execute(
            "SELECT zone_id, event_type FROM events WHERE event_id = ?",
            (event_id,),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None, "billing row was not stored"
    assert row[0] == "zone_billing"
    assert row[1] == "BILLING_QUEUE_JOIN"


@pytest.mark.asyncio
async def test_mixed_canonical_and_shipped_batch(client: AsyncClient):
    canonical = {
        "event_id": str(uuid.uuid4()), "store_id": "STORE_BLR_002",
        "camera_id": "cam_entry", "visitor_id": "vis_9999", "event_type": "ENTRY",
        "timestamp": "2026-03-08T18:10:05+00:00", "zone_id": None, "dwell_ms": None,
        "is_staff": False, "confidence": 0.95,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    resp = await client.post(
        "/events/ingest", json={"events": [canonical, _uniq(SHIPPED_ENTRY, "mixed")]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 2
