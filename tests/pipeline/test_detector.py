# PROMPT: Write tests for a YOLO-based person detection wrapper. The wrapper
#   must: filter to COCO class 0 (person) only, apply confidence threshold
#   (inclusive >=), and call the injected model exactly once per frame. Use a
#   plain Python callable as the mock model — no ultralytics dependency.
#   Include: zero detections, person-only, mixed classes, confidence at/below
#   threshold, and a frame-capture count assertion.
#
# CHANGES MADE: Used a plain callable mock instead of MagicMock to avoid
#   torch/ultralytics import in test context. Threshold test uses exact boundary
#   value (confidence == conf_threshold) which must pass (inclusive).
"""
Phase 4 Tests — Task 5a: PersonDetector  (RED phase)

PersonDetector wraps any YOLO-compatible model and returns only person-class
detections that meet the confidence threshold.

Contract:
  detect(frame_bgr)
    → list[{"bbox": [x1, y1, x2, y2], "confidence": float}]
    → filters to class_id == 0 (COCO "person") only
    → filters to confidence >= conf_threshold (inclusive)
    → calls the injected model exactly once per frame

No weights are downloaded.  The model is a plain Python callable that
returns a list of dicts — identical to what a real YOLO adapter would
produce after parsing ultralytics' Results object.

Mock model protocol (mirrors the adapter layer, not ultralytics internals):
    model(frame_bgr) -> list[{
        "bbox":       [x1, y1, x2, y2],   # xyxy pixel coords
        "confidence": float,               # 0–1
        "class_id":   int,                 # COCO class (0 = person)
    }]

Prompt used to design adversarial mock fixtures (AI-assisted):
  "Write mocks for a YOLO detection wrapper. Include: zero detections,
   person-only, mixed person/non-person classes, confidence at/below
   threshold, and a frame-capture assertion."
"""
from __future__ import annotations

import numpy as np
import pytest

from detector import PersonDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)   # synthetic black frame


def _model(*detections: dict):
    """Return a callable that ignores the frame and yields *detections*."""
    def _fn(frame_bgr: np.ndarray) -> list[dict]:
        return list(detections)
    return _fn


def _person(bbox=(100, 200, 200, 400), confidence=0.9) -> dict:
    return {"bbox": list(bbox), "confidence": confidence, "class_id": 0}


def _nonperson(bbox=(300, 100, 400, 300), confidence=0.9) -> dict:
    """class_id=2 = COCO 'car' — must always be filtered out."""
    return {"bbox": list(bbox), "confidence": confidence, "class_id": 2}


@pytest.fixture
def detector():
    return PersonDetector(_model(), conf_threshold=0.5)


# ---------------------------------------------------------------------------
# Return type / shape
# ---------------------------------------------------------------------------

class TestReturnContract:

    def test_detect_always_returns_list(self, detector):
        result = detector.detect(_FRAME)
        assert isinstance(result, list), "detect() must always return a list"

    def test_detect_no_detections_returns_empty_list(self, detector):
        """Model returns nothing → empty list, not None or []."""
        result = detector.detect(_FRAME)
        assert result == []

    def test_detect_result_dicts_have_bbox_and_confidence_keys(self):
        """Each result dict must contain exactly 'bbox' and 'confidence'."""
        det = PersonDetector(_model(_person()), conf_threshold=0.5)
        result = det.detect(_FRAME)
        assert len(result) == 1
        assert set(result[0].keys()) == {"bbox", "confidence"}, (
            f"Expected keys {{'bbox','confidence'}}, got {set(result[0].keys())}"
        )

    def test_detect_bbox_is_four_element_list(self):
        """bbox must be a four-element sequence [x1, y1, x2, y2]."""
        det = PersonDetector(_model(_person(bbox=(10, 20, 110, 220))), conf_threshold=0.5)
        result = det.detect(_FRAME)
        assert len(result[0]["bbox"]) == 4

    def test_detect_bbox_values_match_model_output(self):
        bbox = (10, 20, 110, 220)
        det = PersonDetector(_model(_person(bbox=bbox)), conf_threshold=0.5)
        result = det.detect(_FRAME)
        assert list(result[0]["bbox"]) == list(bbox)

    def test_detect_confidence_value_matches_model_output(self):
        det = PersonDetector(_model(_person(confidence=0.77)), conf_threshold=0.5)
        result = det.detect(_FRAME)
        assert result[0]["confidence"] == pytest.approx(0.77)


# ---------------------------------------------------------------------------
# Class filtering
# ---------------------------------------------------------------------------

class TestClassFiltering:

    def test_person_class_id_0_is_returned(self):
        det = PersonDetector(_model(_person()), conf_threshold=0.5)
        assert len(det.detect(_FRAME)) == 1

    def test_non_person_class_filtered_out(self):
        """class_id != 0 must be excluded regardless of confidence."""
        det = PersonDetector(_model(_nonperson(confidence=0.99)), conf_threshold=0.5)
        assert det.detect(_FRAME) == []

    def test_mixed_batch_only_persons_returned(self):
        """2 persons + 1 non-person → exactly 2 results."""
        det = PersonDetector(
            _model(_person(), _nonperson(), _person(bbox=(0, 0, 50, 100))),
            conf_threshold=0.5,
        )
        result = det.detect(_FRAME)
        assert len(result) == 2

    def test_result_dicts_do_not_contain_class_id(self):
        """class_id must not leak into the returned dicts."""
        det = PersonDetector(_model(_person()), conf_threshold=0.5)
        result = det.detect(_FRAME)
        assert "class_id" not in result[0], (
            "class_id is an internal field; it must not appear in detect() output"
        )


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------

class TestConfidenceThreshold:

    def test_detection_above_threshold_included(self):
        det = PersonDetector(_model(_person(confidence=0.8)), conf_threshold=0.5)
        assert len(det.detect(_FRAME)) == 1

    def test_detection_at_exact_threshold_included(self):
        """conf == threshold is inclusive (>=)."""
        det = PersonDetector(_model(_person(confidence=0.5)), conf_threshold=0.5)
        assert len(det.detect(_FRAME)) == 1, (
            "Detection at exactly the threshold must be included (>= not >)"
        )

    def test_detection_below_threshold_excluded(self):
        det = PersonDetector(_model(_person(confidence=0.49)), conf_threshold=0.5)
        assert det.detect(_FRAME) == []

    def test_custom_threshold_applied_correctly(self):
        """With threshold=0.9, only high-confidence detections pass."""
        det = PersonDetector(
            _model(_person(confidence=0.95), _person(confidence=0.85)),
            conf_threshold=0.9,
        )
        result = det.detect(_FRAME)
        assert len(result) == 1
        assert result[0]["confidence"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Model interaction
# ---------------------------------------------------------------------------

class TestModelInteraction:

    def test_model_called_once_per_detect_call(self):
        """detect() must invoke the model exactly once."""
        call_log = []

        def recording_model(frame):
            call_log.append(frame)
            return []

        det = PersonDetector(recording_model, conf_threshold=0.5)
        det.detect(_FRAME)
        assert len(call_log) == 1

    def test_model_receives_the_input_frame(self):
        """The exact frame ndarray passed to detect() must reach the model."""
        received = []

        def capturing_model(frame):
            received.append(frame)
            return []

        det = PersonDetector(capturing_model, conf_threshold=0.5)
        det.detect(_FRAME)
        assert np.array_equal(received[0], _FRAME), (
            "Model must receive the exact frame passed to detect()"
        )
