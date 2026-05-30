"""
Phase 4 Tests — Task 5b: PersonTracker  (RED phase)

PersonTracker wraps a ByteTrack-compatible tracker algorithm and adds
centroid computation to every track, which the state machine needs to
decide zone membership.

Contract:
  update(detections, frame_bgr)
    → list[{
        "track_id":  int,
        "bbox":      [x1, y1, x2, y2],
        "confidence": float,
        "centroid":  (float, float),    # ((x1+x2)/2, (y1+y2)/2)
      }]
    → calls the injected tracker callable exactly once per update()
    → refreshes get_active_ids() after every update()

  get_active_ids() → set[int]
    → returns track_ids present in the most recent update()
    → returns empty set before first update()

No model weights are downloaded.  The tracker is a plain Python callable
that returns a list of dicts — the same format a real ByteTrack adapter
would produce after parsing ultralytics' track results.

Tracker callable protocol (mirrors the adapter, not ByteTrack internals):
    tracker(detections, frame_bgr) -> list[{
        "track_id":   int,
        "bbox":       [x1, y1, x2, y2],
        "confidence": float,
    }]

Prompt used to design centroid boundary fixtures (AI-assisted):
  "Give me bounding boxes whose midpoints are exact integers and whose
   midpoints are not integers, to test both the happy path and float
   arithmetic in a centroid computation."
"""
from __future__ import annotations

import numpy as np
import pytest

from tracker import PersonTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def _mock_tracker(*tracks: dict):
    """Return a callable that ignores detections/frame and yields *tracks*."""
    def _fn(detections: list, frame_bgr: np.ndarray) -> list[dict]:
        return list(tracks)
    return _fn


def _track(track_id=1, bbox=(100, 200, 200, 400), confidence=0.9) -> dict:
    return {"track_id": track_id, "bbox": list(bbox), "confidence": confidence}


@pytest.fixture
def tracker():
    return PersonTracker(_mock_tracker())


# ---------------------------------------------------------------------------
# Return type / shape
# ---------------------------------------------------------------------------

class TestReturnContract:

    def test_update_returns_list(self, tracker):
        result = tracker.update([], _FRAME)
        assert isinstance(result, list)

    def test_update_empty_tracks_returns_empty_list(self, tracker):
        result = tracker.update([], _FRAME)
        assert result == []

    def test_update_result_has_required_fields(self):
        t = PersonTracker(_mock_tracker(_track()))
        result = t.update([], _FRAME)
        required = {"track_id", "bbox", "confidence", "centroid"}
        missing = required - result[0].keys()
        assert not missing, f"Track result missing fields: {missing}"

    def test_update_track_id_matches_tracker_output(self):
        t = PersonTracker(_mock_tracker(_track(track_id=42)))
        result = t.update([], _FRAME)
        assert result[0]["track_id"] == 42

    def test_update_bbox_matches_tracker_output(self):
        bbox = [10, 20, 110, 220]
        t = PersonTracker(_mock_tracker(_track(bbox=bbox)))
        result = t.update([], _FRAME)
        assert list(result[0]["bbox"]) == bbox

    def test_update_confidence_matches_tracker_output(self):
        t = PersonTracker(_mock_tracker(_track(confidence=0.77)))
        result = t.update([], _FRAME)
        assert result[0]["confidence"] == pytest.approx(0.77)


# ---------------------------------------------------------------------------
# Centroid computation
# ---------------------------------------------------------------------------

class TestCentroid:

    def test_centroid_is_midpoint_of_bbox(self):
        """centroid = ((x1+x2)/2, (y1+y2)/2)."""
        t = PersonTracker(_mock_tracker(_track(bbox=(100, 200, 200, 400))))
        result = t.update([], _FRAME)
        cx, cy = result[0]["centroid"]
        assert cx == pytest.approx(150.0), f"Expected cx=150.0, got {cx}"
        assert cy == pytest.approx(300.0), f"Expected cy=300.0, got {cy}"

    def test_centroid_integer_midpoint(self):
        """Even-sized bbox → exact integer midpoint, returned as float."""
        t = PersonTracker(_mock_tracker(_track(bbox=(0, 0, 100, 100))))
        result = t.update([], _FRAME)
        cx, cy = result[0]["centroid"]
        assert cx == pytest.approx(50.0)
        assert cy == pytest.approx(50.0)

    def test_centroid_non_integer_midpoint(self):
        """Odd-sized bbox → midpoint is a float (not truncated)."""
        t = PersonTracker(_mock_tracker(_track(bbox=(0, 0, 101, 101))))
        result = t.update([], _FRAME)
        cx, cy = result[0]["centroid"]
        assert cx == pytest.approx(50.5), f"Expected cx=50.5, got {cx}"
        assert cy == pytest.approx(50.5)

    def test_centroid_is_tuple_of_two_floats(self):
        t = PersonTracker(_mock_tracker(_track(bbox=(0, 0, 100, 100))))
        result = t.update([], _FRAME)
        centroid = result[0]["centroid"]
        assert len(centroid) == 2
        assert isinstance(centroid[0], float)
        assert isinstance(centroid[1], float)

    def test_multiple_tracks_each_have_correct_centroid(self):
        """Centroid computed independently per track."""
        tracks = [
            _track(track_id=1, bbox=(0, 0, 100, 100)),    # centroid (50, 50)
            _track(track_id=2, bbox=(200, 300, 400, 500)), # centroid (300, 400)
        ]
        t = PersonTracker(_mock_tracker(*tracks))
        result = t.update([], _FRAME)
        by_id = {r["track_id"]: r for r in result}

        assert by_id[1]["centroid"] == pytest.approx((50.0, 50.0))
        assert by_id[2]["centroid"] == pytest.approx((300.0, 400.0))


# ---------------------------------------------------------------------------
# get_active_ids
# ---------------------------------------------------------------------------

class TestActiveIds:

    def test_get_active_ids_empty_before_first_update(self):
        """Before any update(), there are no active tracks."""
        t = PersonTracker(_mock_tracker())
        assert t.get_active_ids() == set()

    def test_get_active_ids_reflects_last_update(self):
        """After update(), active IDs are those returned by the tracker."""
        tracks = [_track(track_id=1), _track(track_id=2), _track(track_id=3)]
        t = PersonTracker(_mock_tracker(*tracks))
        t.update([], _FRAME)
        assert t.get_active_ids() == {1, 2, 3}

    def test_get_active_ids_refreshed_on_each_update(self):
        """
        A track that disappears after an update must not remain in active IDs.
        This is the signal that flush_exits() uses to detect vanished tracks.
        """
        both_present = _mock_tracker(_track(track_id=1), _track(track_id=2))
        only_one     = _mock_tracker(_track(track_id=1))

        t = PersonTracker(both_present)
        t.update([], _FRAME)
        assert t.get_active_ids() == {1, 2}

        # Swap to a tracker that only returns track 2
        t._tracker = only_one
        t.update([], _FRAME)
        assert t.get_active_ids() == {1}, "Stale track IDs must not persist"

    def test_get_active_ids_returns_set_not_list(self):
        t = PersonTracker(_mock_tracker(_track(track_id=7)))
        t.update([], _FRAME)
        assert isinstance(t.get_active_ids(), set)

    def test_get_active_ids_does_not_mutate_internal_state(self):
        """
        Modifying the returned set must not affect subsequent calls.
        get_active_ids() must return a copy, not the internal reference.
        """
        t = PersonTracker(_mock_tracker(_track(track_id=1)))
        t.update([], _FRAME)
        ids = t.get_active_ids()
        ids.add(999)                           # mutate the returned set
        assert 999 not in t.get_active_ids()  # internal state must be unchanged


# ---------------------------------------------------------------------------
# Tracker interaction
# ---------------------------------------------------------------------------

class TestTrackerInteraction:

    def test_tracker_called_exactly_once_per_update(self):
        """update() must invoke the tracker callable exactly once."""
        call_log = []

        def recording_tracker(detections, frame):
            call_log.append(1)
            return []

        t = PersonTracker(recording_tracker)
        t.update([], _FRAME)
        assert len(call_log) == 1

    def test_tracker_receives_detections_from_update(self):
        """The detections list passed to update() must reach the tracker."""
        received = []

        def capturing_tracker(detections, frame):
            received.append(detections)
            return []

        detections = [{"bbox": [0, 0, 100, 100], "confidence": 0.8}]
        t = PersonTracker(capturing_tracker)
        t.update(detections, _FRAME)
        assert received[0] == detections

    def test_tracker_receives_frame_from_update(self):
        """The frame ndarray passed to update() must reach the tracker."""
        received = []

        def capturing_tracker(detections, frame):
            received.append(frame)
            return []

        t = PersonTracker(capturing_tracker)
        t.update([], _FRAME)
        assert np.array_equal(received[0], _FRAME)
