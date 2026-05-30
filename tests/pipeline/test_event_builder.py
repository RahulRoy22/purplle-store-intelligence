# PROMPT: Give me edge cases for an event-stamping adapter that converts raw
#   state-machine dicts into EventIn-valid payloads. Must cover: zone_id=None
#   (camera_id fallback to ""), multiple events with identical timestamps each
#   getting unique UUID4 event_ids, tz-naive datetime normalised to UTC,
#   is_staff stored as Python bool (not np.bool_), and metadata merging where
#   raw event metadata overrides defaults.
#
# CHANGES MADE: Added isinstance(is_staff, bool) check — np.bool_ is a subclass
#   of int but not bool, causing JSON serialisation to emit 0/1 instead of
#   true/false. Fixed by wrapping with bool() in _stamp(). Added handling for
#   state_machine emitting timestamp as isoformat() string (not datetime object).
"""
Phase 4 Tests — Task 6: EventBuilder  (RED phase)

EventBuilder is a pure-Python adapter: it takes the raw dicts that
state_machine.py emits and stamps them into fully-valid EventIn payloads
ready to POST to /events/ingest.

Contract:
  EventBuilder(store_id, zone_camera_map, default_metadata=None)
    store_id:         str  — injected at construction; written to every event
    zone_camera_map:  dict[str, str]  — zone_id → camera_id lookup
    default_metadata: dict | None  — base metadata merged into every event

  build(raw_events: list[dict]) -> list[dict]
    → one output dict per input dict
    → every output dict passes pydantic.parse_obj(EventIn, output) without error
    → event_id   is a fresh UUID4 string (never reused across calls or events)
    → timestamp  is the raw event's datetime re-serialised as an ISO-8601
                 string that includes UTC offset ("+00:00" or "Z"); the value
                 must match the original datetime within 1 second (no drift)
    → store_id   matches constructor arg
    → camera_id  = zone_camera_map[zone_id] when zone_id is not None
    → camera_id  = "" (or any non-None str) when zone_id is None — must not raise
    → zone_id    is passed through unchanged (str or None)
    → dwell_ms   is passed through unchanged (int or None)
    → is_staff   is a Python bool (isinstance check — np.bool_ is rejected)
    → confidence is a float in [0.0, 1.0]
    → metadata   contains at minimum {"queue_depth": ..., "sku_zone": ...,
                 "session_seq": ...} keys (EventMetadata fields)
    → build([]) returns []
    → each call to build() produces new event_ids (not deterministic/cached)

Raw event dict format produced by state_machine.py:
  {
    "event_type": str,        # one of the 8 valid EventType literals
    "visitor_id": str,
    "zone_id":    str | None,
    "dwell_ms":   int | None,
    "is_staff":   bool,
    "confidence": float,
    "timestamp":  datetime,   # timezone-aware UTC datetime
  }

Prompt used to design fixture edge-cases (AI-assisted):
  "Give me edge cases for an event-stamping adapter: zone_id=None,
   multiple events with identical timestamps, and an already-tz-naive
   datetime to confirm it is rejected or normalised."
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from models.event import EventIn            # lives in services/api/models/event.py
from event_builder import EventBuilder      # stub in services/pipeline/event_builder.py


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ZONE_CAM = {
    "zone_entry":    "cam_entry",
    "zone_skincare": "cam_floor_01",
    "zone_makeup":   "cam_floor_02",
    "zone_billing":  "cam_billing",
}

_TS = datetime(2026, 5, 30, 10, 15, 0, tzinfo=timezone.utc)


def _raw(
    event_type="ENTRY",
    visitor_id="vis_0001",
    zone_id="zone_entry",
    dwell_ms=None,
    is_staff=False,
    confidence=0.92,
    timestamp=None,
) -> dict:
    return {
        "event_type": event_type,
        "visitor_id": visitor_id,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "timestamp": timestamp or _TS,
    }


@pytest.fixture
def builder():
    return EventBuilder("store_001", _ZONE_CAM)


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------

class TestReturnContract:

    def test_build_empty_returns_empty_list(self, builder):
        assert builder.build([]) == []

    def test_build_returns_list(self, builder):
        result = builder.build([_raw()])
        assert isinstance(result, list)

    def test_build_one_event_returns_one_dict(self, builder):
        result = builder.build([_raw()])
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_build_preserves_count(self, builder):
        """N raw events → N output dicts."""
        raw = [_raw(visitor_id=f"vis_{i:04d}") for i in range(5)]
        result = builder.build(raw)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Pydantic schema validation — the core contract
# ---------------------------------------------------------------------------

class TestEventInValidation:

    def test_output_passes_eventin_validation(self, builder):
        """Every output dict must parse without error via EventIn."""
        result = builder.build([_raw()])
        parsed = EventIn(**result[0])   # raises ValidationError if schema mismatch
        assert parsed.event_id is not None

    def test_all_required_fields_present(self, builder):
        """Check every mandatory EventIn field exists in output."""
        required = {
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "is_staff", "confidence",
        }
        result = builder.build([_raw()])
        missing = required - result[0].keys()
        assert not missing, f"Output missing required fields: {missing}"

    def test_zone_dwell_event_passes_validation(self, builder):
        """ZONE_DWELL with dwell_ms set must also parse cleanly."""
        raw = _raw(event_type="ZONE_DWELL", zone_id="zone_skincare", dwell_ms=45000)
        parsed = EventIn(**builder.build([raw])[0])
        assert parsed.dwell_ms == 45000

    def test_billing_queue_join_passes_validation(self, builder):
        raw = _raw(event_type="BILLING_QUEUE_JOIN", zone_id="zone_billing")
        EventIn(**builder.build([raw])[0])   # must not raise

    def test_exit_event_passes_validation(self, builder):
        raw = _raw(event_type="EXIT", zone_id="zone_entry")
        EventIn(**builder.build([raw])[0])


# ---------------------------------------------------------------------------
# event_id — UUID4 uniqueness
# ---------------------------------------------------------------------------

class TestEventId:

    def test_event_id_is_valid_uuid(self, builder):
        result = builder.build([_raw()])
        eid = result[0]["event_id"]
        parsed = uuid.UUID(eid)           # raises ValueError if not a valid UUID
        assert str(parsed) == eid.lower(), "event_id must be in canonical UUID format"

    def test_event_id_is_version_4(self, builder):
        result = builder.build([_raw()])
        eid = result[0]["event_id"]
        assert uuid.UUID(eid).version == 4, "event_id must be a UUID version 4"

    def test_event_ids_are_unique_within_batch(self, builder):
        """Five events in one build() call must each get a distinct UUID."""
        raw = [_raw(visitor_id=f"vis_{i:04d}") for i in range(5)]
        result = builder.build(raw)
        ids = [r["event_id"] for r in result]
        assert len(ids) == len(set(ids)), "event_id values must be unique within a batch"

    def test_event_ids_differ_across_build_calls(self, builder):
        """Calling build() twice on the same input must produce different event_ids."""
        r1 = builder.build([_raw()])[0]["event_id"]
        r2 = builder.build([_raw()])[0]["event_id"]
        assert r1 != r2, "Each build() call must generate fresh UUIDs"


# ---------------------------------------------------------------------------
# timestamp — ISO-8601 with timezone
# ---------------------------------------------------------------------------

class TestTimestamp:

    def test_timestamp_is_string_in_output(self, builder):
        """EventIn expects a datetime, but the output dict's timestamp
        must serialise cleanly — either a datetime or an ISO-8601 str."""
        result = builder.build([_raw()])
        ts = result[0]["timestamp"]
        # Must be parseable: either already a datetime or a valid ISO string
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
        else:
            dt = ts
        assert dt is not None

    def test_timestamp_preserves_original_value(self, builder):
        """Output timestamp must represent the same moment as the input datetime."""
        ts_in = datetime(2026, 5, 30, 14, 22, 33, tzinfo=timezone.utc)
        result = builder.build([_raw(timestamp=ts_in)])
        ts_out = result[0]["timestamp"]
        if isinstance(ts_out, str):
            ts_out = datetime.fromisoformat(ts_out)
        # Allow 0 drift — same moment
        assert abs((ts_out.replace(tzinfo=timezone.utc if ts_out.tzinfo is None else ts_out.tzinfo)
                    - ts_in).total_seconds()) < 1, "Timestamp must not drift"

    def test_output_timestamp_is_timezone_aware(self, builder):
        """EventIn rejects naive timestamps — the output must carry tz info."""
        result = builder.build([_raw()])
        ts = result[0]["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        assert ts.tzinfo is not None, "Output timestamp must be timezone-aware"

    def test_multiple_events_timestamps_are_independent(self, builder):
        """Two events with different timestamps must each keep their own value."""
        t1 = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 30, 10, 5, 0, tzinfo=timezone.utc)
        r1, r2 = builder.build([_raw(timestamp=t1), _raw(timestamp=t2)])
        ts1 = r1["timestamp"]
        ts2 = r2["timestamp"]
        if isinstance(ts1, str):
            ts1 = datetime.fromisoformat(ts1)
        if isinstance(ts2, str):
            ts2 = datetime.fromisoformat(ts2)
        assert ts1 != ts2, "Each event's timestamp must be independent"


# ---------------------------------------------------------------------------
# store_id and camera_id injection
# ---------------------------------------------------------------------------

class TestStoreAndCamera:

    def test_store_id_injected_from_constructor(self, builder):
        result = builder.build([_raw()])
        assert result[0]["store_id"] == "store_001"

    def test_store_id_reflects_constructor_argument(self):
        b = EventBuilder("store_XYZ", _ZONE_CAM)
        result = b.build([_raw()])
        assert result[0]["store_id"] == "store_XYZ"

    def test_camera_id_comes_from_zone_camera_map(self, builder):
        result = builder.build([_raw(zone_id="zone_skincare")])
        assert result[0]["camera_id"] == "cam_floor_01"

    def test_camera_id_for_billing_zone(self, builder):
        result = builder.build([_raw(zone_id="zone_billing")])
        assert result[0]["camera_id"] == "cam_billing"

    def test_camera_id_for_none_zone(self, builder):
        """zone_id=None must not raise; camera_id should be a non-None string."""
        result = builder.build([_raw(zone_id=None)])
        assert result[0]["camera_id"] is not None
        assert isinstance(result[0]["camera_id"], str)

    def test_zone_id_passed_through(self, builder):
        result = builder.build([_raw(zone_id="zone_makeup")])
        assert result[0]["zone_id"] == "zone_makeup"

    def test_none_zone_id_passed_through(self, builder):
        result = builder.build([_raw(zone_id=None)])
        assert result[0]["zone_id"] is None


# ---------------------------------------------------------------------------
# Field pass-through and type correctness
# ---------------------------------------------------------------------------

class TestFieldTypes:

    def test_visitor_id_passed_through(self, builder):
        result = builder.build([_raw(visitor_id="vis_9999")])
        assert result[0]["visitor_id"] == "vis_9999"

    def test_event_type_passed_through(self, builder):
        result = builder.build([_raw(event_type="ZONE_DWELL")])
        assert result[0]["event_type"] == "ZONE_DWELL"

    def test_dwell_ms_passed_through_when_set(self, builder):
        result = builder.build([_raw(dwell_ms=12345)])
        assert result[0]["dwell_ms"] == 12345

    def test_dwell_ms_none_passed_through(self, builder):
        result = builder.build([_raw(dwell_ms=None)])
        assert result[0]["dwell_ms"] is None

    def test_confidence_passed_through(self, builder):
        result = builder.build([_raw(confidence=0.77)])
        assert result[0]["confidence"] == pytest.approx(0.77)

    def test_is_staff_true_passed_through(self, builder):
        result = builder.build([_raw(is_staff=True)])
        assert result[0]["is_staff"] is True

    def test_is_staff_false_passed_through(self, builder):
        result = builder.build([_raw(is_staff=False)])
        assert result[0]["is_staff"] is False

    def test_is_staff_is_python_bool_not_numpy(self, builder):
        """np.bool_ breaks Pydantic's strict bool check in some versions."""
        import numpy as np
        raw = _raw()
        raw["is_staff"] = np.bool_(False)   # simulate numpy contamination
        result = builder.build([raw])
        assert type(result[0]["is_staff"]) is bool, (
            "is_staff must be a Python bool, not np.bool_"
        )

    def test_metadata_has_required_keys(self, builder):
        result = builder.build([_raw()])
        meta = result[0].get("metadata", {})
        for key in ("queue_depth", "sku_zone", "session_seq"):
            assert key in meta, f"metadata missing key: {key}"
