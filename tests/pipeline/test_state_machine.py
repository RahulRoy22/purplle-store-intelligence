"""
Phase 4 Tests — Task 3: TrackStateMachine  (RED phase)

TrackStateMachine converts raw per-frame tracker output into the EventIn
payloads that POST /events/ingest expects.

Contract (per the Phase 4 architectural proposal):
  update_track(track_id, zone_id, is_staff, confidence, frame_ts, visitor_id=None)
    → list[dict]   — events to emit for this track update

  flush_exits(active_track_ids, frame_ts)
    → list[dict]   — EXIT events for tracks not seen this frame

Event-type rules verified:
  ENTRY         First time a track_id is seen
  REENTRY       When caller provides an existing visitor_id (Re-ID match)
  ZONE_ENTER    Centroid moves into a zone (zone_id changes from None or other zone)
  ZONE_DWELL    Emitted WITH ZONE_EXIT when leaving a zone; dwell_ms = elapsed ms
  ZONE_EXIT     Same trigger as ZONE_DWELL
  BILLING_QUEUE_JOIN     Emitted when entering zone_billing specifically
  BILLING_QUEUE_ABANDON  Emitted when leaving zone_billing
  EXIT          Emitted by flush_exits for vanished tracks

Staff rule: ENTRY / ZONE_ENTER / etc. are still emitted for staff tracks
(is_staff=True) — filtering happens at query time, not at ingest.

Prompt used to design adversarial scenarios (AI-assisted):
  "Write pytest scenarios for a track state machine. Include: REENTRY via
   visitor_id parameter, dwell accumulation across multiple frames,
   BILLING_QUEUE_JOIN queue_depth metadata, rapid zone switches, and
   flush_exits for simultaneous vanished tracks."
"""
import uuid
import pytest
from datetime import datetime, timezone, timedelta

from state_machine import TrackStateMachine


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

STORE    = "store_test"
CAMERA   = "cam_test"
BASE_TS  = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)


def _ts(offset_ms: int = 0) -> datetime:
    return BASE_TS + timedelta(milliseconds=offset_ms)


@pytest.fixture
def sm():
    return TrackStateMachine(STORE, CAMERA)


def _event_types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


def _first(events: list[dict], event_type: str) -> dict:
    """Return first event of given type, or raise AssertionError."""
    matches = [e for e in events if e["event_type"] == event_type]
    assert matches, f"No {event_type!r} event found in {_event_types(events)}"
    return matches[0]


# ---------------------------------------------------------------------------
# ENTRY
# ---------------------------------------------------------------------------

class TestEntry:

    def test_first_update_emits_entry(self, sm):
        """First time a track_id appears → ENTRY event."""
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        assert "ENTRY" in _event_types(events), (
            f"Expected ENTRY on first appearance, got {_event_types(events)}"
        )

    def test_entry_has_all_required_fields(self, sm):
        """ENTRY event must contain every field required by the EventIn schema."""
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        entry = _first(events, "ENTRY")
        required = {
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "zone_id", "dwell_ms",
            "is_staff", "confidence", "metadata",
        }
        missing = required - entry.keys()
        assert not missing, f"ENTRY event missing fields: {missing}"

    def test_entry_event_id_is_valid_uuid(self, sm):
        """event_id must be a valid UUID string."""
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        entry = _first(events, "ENTRY")
        uuid.UUID(entry["event_id"])   # raises ValueError if invalid

    def test_entry_visitor_id_is_valid_uuid(self, sm):
        """visitor_id must be a valid UUID string assigned per track."""
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        entry = _first(events, "ENTRY")
        uuid.UUID(entry["visitor_id"])

    def test_entry_store_id_matches(self, sm):
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        assert _first(events, "ENTRY")["store_id"] == STORE

    def test_entry_camera_id_matches(self, sm):
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        assert _first(events, "ENTRY")["camera_id"] == CAMERA

    def test_entry_timestamp_is_timezone_aware(self, sm):
        """timestamp must include timezone info (ISO-8601 with offset)."""
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts())
        ts_str = _first(events, "ENTRY")["timestamp"]
        parsed = datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None, "timestamp must be timezone-aware"

    def test_second_update_same_track_no_extra_entry(self, sm):
        """A track seen twice should not produce a second ENTRY."""
        sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        events2 = sm.update_track(1, "zone_entry", False, 0.95, _ts(500))
        assert "ENTRY" not in _event_types(events2), (
            "ENTRY must only be emitted on first appearance"
        )

    def test_different_track_ids_get_different_visitor_ids(self, sm):
        """Each distinct track_id must have its own visitor_id."""
        e1 = sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        e2 = sm.update_track(2, "zone_entry", False, 0.95, _ts(10))
        vid1 = _first(e1, "ENTRY")["visitor_id"]
        vid2 = _first(e2, "ENTRY")["visitor_id"]
        assert vid1 != vid2, "Different track_ids must not share visitor_ids"

    def test_staff_track_still_emits_entry(self, sm):
        """
        Staff tracks (is_staff=True) must still produce ENTRY —
        filtering is the analytics layer's job, not the pipeline's.
        """
        events = sm.update_track(99, "zone_entry", True, 0.99, _ts())
        entry = _first(events, "ENTRY")
        assert entry["is_staff"] is True


# ---------------------------------------------------------------------------
# REENTRY
# ---------------------------------------------------------------------------

class TestReentry:

    def test_provided_visitor_id_produces_reentry(self, sm):
        """
        When Re-ID matches a previous visitor, the caller passes visitor_id.
        The state machine must emit REENTRY (not ENTRY) for this track.
        """
        existing_vid = str(uuid.uuid4())
        events = sm.update_track(
            5, "zone_entry", False, 0.92, _ts(), visitor_id=existing_vid
        )
        types = _event_types(events)
        assert "REENTRY" in types, f"Expected REENTRY, got {types}"
        assert "ENTRY" not in types, "REENTRY must not be accompanied by ENTRY"

    def test_reentry_uses_provided_visitor_id(self, sm):
        """visitor_id in REENTRY event must be the provided one (Re-ID match)."""
        existing_vid = str(uuid.uuid4())
        events = sm.update_track(
            5, "zone_entry", False, 0.92, _ts(), visitor_id=existing_vid
        )
        reentry = _first(events, "REENTRY")
        assert reentry["visitor_id"] == existing_vid


# ---------------------------------------------------------------------------
# ZONE_ENTER / ZONE_DWELL / ZONE_EXIT
# ---------------------------------------------------------------------------

class TestZoneTransitions:

    def test_entering_zone_emits_zone_enter(self, sm):
        """Track moves from outside store to inside a zone → ZONE_ENTER."""
        sm.update_track(1, None, False, 0.95, _ts(0))    # enters store, no zone yet
        events = sm.update_track(1, "zone_skincare", False, 0.95, _ts(500))
        assert "ZONE_ENTER" in _event_types(events)

    def test_zone_enter_carries_zone_id(self, sm):
        sm.update_track(1, None, False, 0.95, _ts(0))
        events = sm.update_track(1, "zone_skincare", False, 0.95, _ts(500))
        ze = _first(events, "ZONE_ENTER")
        assert ze["zone_id"] == "zone_skincare"

    def test_leaving_zone_emits_zone_dwell_and_exit(self, sm):
        """
        Track in zone_skincare for 2 000 ms then moves away →
        emits ZONE_DWELL + ZONE_EXIT.
        """
        sm.update_track(1, "zone_entry",   False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(1000))   # enters zone
        events = sm.update_track(1, None, False, 0.95, _ts(3000))     # leaves zone
        types = _event_types(events)
        assert "ZONE_DWELL" in types, f"Expected ZONE_DWELL, got {types}"
        assert "ZONE_EXIT"  in types, f"Expected ZONE_EXIT, got {types}"

    def test_zone_dwell_ms_matches_elapsed_time(self, sm):
        """
        dwell_ms in ZONE_DWELL must equal the milliseconds between
        ZONE_ENTER timestamp and ZONE_EXIT timestamp.
        """
        enter_ms = 1000
        exit_ms  = 3500     # 2500 ms dwell
        sm.update_track(1, "zone_entry",    False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(enter_ms))
        events = sm.update_track(1, None,   False, 0.95, _ts(exit_ms))
        dwell_event = _first(events, "ZONE_DWELL")
        assert dwell_event["dwell_ms"] == exit_ms - enter_ms, (
            f"Expected dwell_ms={exit_ms - enter_ms}, got {dwell_event['dwell_ms']}"
        )

    def test_dwell_accumulates_across_multiple_frames_in_same_zone(self, sm):
        """
        Track stays in zone_skincare for 3 frames before leaving.
        dwell_ms must cover the full span, not just the last frame gap.
        """
        sm.update_track(1, "zone_entry",    False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(1000))   # enter at 1s
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(2000))   # still in zone
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(3000))   # still in zone
        events = sm.update_track(1, None,   False, 0.95, _ts(4000))   # leave at 4s

        dwell = _first(events, "ZONE_DWELL")
        assert dwell["dwell_ms"] == 3000, (
            f"Dwell must span full 3000ms in zone, got {dwell['dwell_ms']}ms"
        )

    def test_zone_change_emits_exit_from_old_and_enter_to_new(self, sm):
        """
        Track moves directly from zone_skincare to zone_makeup (no gap) →
        ZONE_EXIT + ZONE_DWELL for old zone, ZONE_ENTER for new zone.
        """
        sm.update_track(1, "zone_entry",    False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(500))
        events = sm.update_track(1, "zone_makeup", False, 0.95, _ts(1500))
        types = _event_types(events)
        assert "ZONE_EXIT"  in types
        assert "ZONE_DWELL" in types
        assert "ZONE_ENTER" in types

    def test_staying_in_same_zone_does_not_re_emit_zone_enter(self, sm):
        """No duplicate ZONE_ENTER for a track that stays in the same zone."""
        sm.update_track(1, "zone_entry",    False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(500))
        events2 = sm.update_track(1, "zone_skincare", False, 0.95, _ts(1000))
        assert "ZONE_ENTER" not in _event_types(events2), (
            "ZONE_ENTER must not repeat while track stays in the same zone"
        )


# ---------------------------------------------------------------------------
# BILLING_QUEUE_JOIN / BILLING_QUEUE_ABANDON
# ---------------------------------------------------------------------------

class TestBillingZone:

    def test_entering_billing_zone_emits_billing_queue_join(self, sm):
        """Moving into zone_billing emits BILLING_QUEUE_JOIN (in addition to ZONE_ENTER)."""
        sm.update_track(1, "zone_entry",   False, 0.95, _ts(0))
        events = sm.update_track(1, "zone_billing", False, 0.95, _ts(600_000))
        assert "BILLING_QUEUE_JOIN" in _event_types(events), (
            f"Expected BILLING_QUEUE_JOIN when entering zone_billing, got {_event_types(events)}"
        )

    def test_billing_queue_join_includes_queue_depth_in_metadata(self, sm):
        """
        metadata.queue_depth must be set on BILLING_QUEUE_JOIN to the number
        of visitors currently in zone_billing (including this visitor).
        """
        sm.update_track(1, "zone_billing", False, 0.95, _ts(0))
        events = sm.update_track(2, "zone_billing", False, 0.95, _ts(500))
        join = _first(events, "BILLING_QUEUE_JOIN")
        meta = join.get("metadata", {})
        assert "queue_depth" in meta, "BILLING_QUEUE_JOIN metadata must contain queue_depth"
        assert isinstance(meta["queue_depth"], int)
        assert meta["queue_depth"] >= 1

    def test_queue_depth_reflects_concurrent_billing_visitors(self, sm):
        """
        When 3 visitors are in zone_billing, a new join must report queue_depth >= 3.
        """
        sm.update_track(1, "zone_billing", False, 0.95, _ts(0))
        sm.update_track(2, "zone_billing", False, 0.95, _ts(10))
        sm.update_track(3, "zone_billing", False, 0.95, _ts(20))
        events = sm.update_track(4, "zone_billing", False, 0.95, _ts(30))
        join = _first(events, "BILLING_QUEUE_JOIN")
        assert join["metadata"]["queue_depth"] >= 3, (
            "queue_depth must account for all concurrent billing visitors"
        )

    def test_leaving_billing_zone_emits_billing_queue_abandon(self, sm):
        """
        Track leaves zone_billing → BILLING_QUEUE_ABANDON emitted (in addition
        to ZONE_DWELL + ZONE_EXIT). The analytics layer correlates with POS data.
        """
        sm.update_track(1, "zone_entry",   False, 0.95, _ts(0))
        sm.update_track(1, "zone_billing", False, 0.95, _ts(600_000))
        events = sm.update_track(1, None,  False, 0.95, _ts(660_000))
        assert "BILLING_QUEUE_ABANDON" in _event_types(events), (
            f"Expected BILLING_QUEUE_ABANDON when leaving zone_billing, "
            f"got {_event_types(events)}"
        )

    def test_non_billing_zone_exit_does_not_emit_billing_abandon(self, sm):
        """Leaving zone_skincare must NOT produce BILLING_QUEUE_ABANDON."""
        sm.update_track(1, "zone_entry",    False, 0.95, _ts(0))
        sm.update_track(1, "zone_skincare", False, 0.95, _ts(500))
        events = sm.update_track(1, None,   False, 0.95, _ts(3000))
        assert "BILLING_QUEUE_ABANDON" not in _event_types(events)


# ---------------------------------------------------------------------------
# EXIT  (flush_exits)
# ---------------------------------------------------------------------------

class TestFlushExits:

    def test_flush_exits_emits_exit_for_vanished_track(self, sm):
        """
        Track 1 was active but is absent from active_track_ids →
        flush_exits must emit EXIT.
        """
        sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        events = sm.flush_exits(active_track_ids=set(), frame_ts=_ts(5000))
        assert "EXIT" in _event_types(events)

    def test_flush_exits_still_present_track_no_exit(self, sm):
        """Track in active_track_ids must not receive EXIT."""
        sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        events = sm.flush_exits(active_track_ids={1}, frame_ts=_ts(5000))
        assert "EXIT" not in _event_types(events)

    def test_flush_exits_multiple_vanished_tracks(self, sm):
        """3 tracks vanish at once → 3 EXIT events."""
        for tid in [1, 2, 3]:
            sm.update_track(tid, "zone_entry", False, 0.95, _ts(0))
        events = sm.flush_exits(active_track_ids=set(), frame_ts=_ts(5000))
        exit_events = [e for e in events if e["event_type"] == "EXIT"]
        assert len(exit_events) == 3

    def test_flush_exits_clears_track_from_state(self, sm):
        """
        After flush_exits removes a track, re-appearing with the same track_id
        is treated as a brand-new ENTRY (not a duplicate).
        """
        sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        sm.flush_exits(active_track_ids=set(), frame_ts=_ts(5000))
        # Re-appear — should produce ENTRY again
        events = sm.update_track(1, "zone_entry", False, 0.95, _ts(10_000))
        assert "ENTRY" in _event_types(events), (
            "A track that re-appears after EXIT should produce a new ENTRY"
        )

    def test_exit_event_has_required_fields(self, sm):
        """EXIT event produced by flush_exits must have all EventIn fields."""
        sm.update_track(1, "zone_entry", False, 0.95, _ts(0))
        events = sm.flush_exits(active_track_ids=set(), frame_ts=_ts(5000))
        exit_ev = _first(events, "EXIT")
        required = {
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "zone_id", "dwell_ms",
            "is_staff", "confidence", "metadata",
        }
        missing = required - exit_ev.keys()
        assert not missing, f"EXIT event missing fields: {missing}"

    def test_flush_exits_empty_state_returns_empty_list(self, sm):
        """Calling flush_exits with no tracked visitors returns []."""
        events = sm.flush_exits(active_track_ids=set(), frame_ts=_ts(0))
        assert events == []
