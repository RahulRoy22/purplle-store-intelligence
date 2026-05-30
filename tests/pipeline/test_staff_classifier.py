# PROMPT: Write pytest fixtures for an HSV-based staff uniform classifier.
#   The classifier returns True when the fraction of pixels matching the HSV
#   range meets or exceeds a threshold. Cover: solid uniform colour (all pixels
#   match → True), solid non-uniform colour (0 pixels match → False), partial
#   uniform at 19%/25%/26% relative to a 25% threshold, all-black crop, and
#   a 1×1-pixel crop. Verify the return value is Python bool, not np.bool_.
#
# CHANGES MADE: return bool() cast added in is_staff() after discovering
#   numpy comparison returns np.bool_ which fails `is True` identity checks
#   in test assertions.
"""
Phase 4 Tests — Task 2: StaffClassifier  (RED phase)

StaffClassifier analyses a BGR image crop and returns True when the crop's
dominant colour falls within the staff-uniform HSV range.

Contract:
  - Crop dominated by uniform colour   → True
  - Crop with no matching colour       → False
  - Pixel ratio < threshold            → False
  - Pixel ratio == threshold           → True  (inclusive lower bound)
  - Pixel ratio > threshold            → True
  - Empty / 1-pixel crop               → False  (no crash)
  - from_config(dict)                  → factory with correct params

Test strategy: synthetic numpy crops with exact colour composition are used
instead of real images so edge-case boundaries are mathematically precise.

Prompt used to design boundary fixtures (AI-assisted):
  "Write pytest fixtures for an HSV threshold classifier. Cover: solid
   uniform colour, solid non-uniform colour, partial uniform at 19%/25%/26%,
   all-black crop, and a 1×1-pixel crop."
"""
import numpy as np
import pytest

from staff_classifier import StaffClassifier


# ---------------------------------------------------------------------------
# Fixed HSV range representing a blue staff uniform
# Matches cv2 convention: H in [0,179], S and V in [0,255]
# Pure blue (BGR [255,0,0]) converts to HSV ≈ [120, 255, 255]
# ---------------------------------------------------------------------------

_HSV_LOWER = np.array([100, 80, 50],  dtype=np.uint8)
_HSV_UPPER = np.array([130, 255, 255], dtype=np.uint8)
_THRESHOLD  = 0.25


# ---------------------------------------------------------------------------
# Helper: build synthetic BGR crops
# ---------------------------------------------------------------------------

def _solid(bgr: tuple[int, int, int], h: int = 50, w: int = 50) -> np.ndarray:
    """Return a solid-colour BGR image of given dimensions."""
    return np.full((h, w, 3), bgr, dtype=np.uint8)


def _partial_blue(pct: float, h: int = 100, w: int = 100) -> np.ndarray:
    """
    Return a crop where `pct` fraction of rows are blue, rest are red.
    E.g. pct=0.25 → top 25 rows blue, bottom 75 rows red.
    """
    crop = _solid((0, 0, 255), h, w)           # start all-red (BGR)
    blue_rows = int(round(pct * h))
    crop[:blue_rows, :] = (255, 0, 0)          # overwrite top rows with blue
    return crop


@pytest.fixture
def classifier():
    return StaffClassifier(_HSV_LOWER, _HSV_UPPER, _THRESHOLD)


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

class TestDetection:

    def test_solid_blue_crop_is_staff(self, classifier):
        """100% uniform-colour crop → True."""
        crop = _solid((255, 0, 0))              # pure blue in BGR
        assert classifier.is_staff(crop) is True

    def test_solid_red_crop_is_not_staff(self, classifier):
        """Red (H≈0) is outside the blue range → False."""
        crop = _solid((0, 0, 255))              # pure red in BGR
        assert classifier.is_staff(crop) is False

    def test_solid_green_crop_is_not_staff(self, classifier):
        """Green (H≈60) is outside the blue range → False."""
        crop = _solid((0, 255, 0))              # pure green in BGR
        assert classifier.is_staff(crop) is False

    def test_all_black_crop_is_not_staff(self, classifier):
        """Black has zero saturation → not matched by any uniform colour range."""
        crop = _solid((0, 0, 0))
        assert classifier.is_staff(crop) is False

    def test_all_white_crop_is_not_staff(self, classifier):
        """White has zero saturation → not matched."""
        crop = _solid((255, 255, 255))
        assert classifier.is_staff(crop) is False


# ---------------------------------------------------------------------------
# Threshold boundary
# ---------------------------------------------------------------------------

class TestThreshold:

    def test_below_threshold_is_not_staff(self, classifier):
        """
        19% blue pixels, threshold=25% → False.
        Verifies the classifier does not fire on incidental blue background noise.
        """
        crop = _partial_blue(0.19)
        assert classifier.is_staff(crop) is False, (
            "19% blue must NOT reach the 25% threshold"
        )

    def test_exactly_at_threshold_is_staff(self, classifier):
        """
        25% blue pixels, threshold=25% → True  (inclusive >=).
        Critical boundary: a staff member partially occluded must still be detected.
        """
        crop = _partial_blue(0.25)
        assert classifier.is_staff(crop) is True, (
            "Exactly 25% blue must be classified as staff (inclusive lower bound)"
        )

    def test_above_threshold_is_staff(self, classifier):
        """80% blue, threshold=25% → True."""
        crop = _partial_blue(0.80)
        assert classifier.is_staff(crop) is True

    def test_custom_threshold_100pct_only_triggers_on_solid(self):
        """
        A threshold of 1.0 means ONLY a fully-uniform crop qualifies.
        """
        clf = StaffClassifier(_HSV_LOWER, _HSV_UPPER, threshold=1.0)
        assert clf.is_staff(_solid((255, 0, 0))) is True
        assert clf.is_staff(_partial_blue(0.99)) is False

    def test_custom_threshold_zero_always_returns_true_for_any_blue(self):
        """
        Threshold 0.0 → even a single blue pixel qualifies.
        """
        clf = StaffClassifier(_HSV_LOWER, _HSV_UPPER, threshold=0.0)
        # One blue pixel in an otherwise red crop
        crop = _solid((0, 0, 255), 10, 10)
        crop[0, 0] = (255, 0, 0)              # one blue pixel
        assert clf.is_staff(crop) is True


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:

    def test_empty_array_returns_false(self, classifier):
        """A zero-element array must not crash — return False."""
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        assert classifier.is_staff(empty) is False

    def test_single_pixel_blue_returns_true(self, classifier):
        """A 1×1 pixel array that is blue → True (100% coverage)."""
        crop = np.array([[[255, 0, 0]]], dtype=np.uint8)   # shape (1,1,3) blue
        assert classifier.is_staff(crop) is True

    def test_single_pixel_red_returns_false(self, classifier):
        """A 1×1 red pixel → False."""
        crop = np.array([[[0, 0, 255]]], dtype=np.uint8)   # shape (1,1,3) red
        assert classifier.is_staff(crop) is False

    def test_does_not_mutate_input_crop(self, classifier):
        """The classifier must not modify the input array (read-only contract)."""
        crop = _solid((255, 0, 0)).copy()
        original = crop.copy()
        classifier.is_staff(crop)
        assert np.array_equal(crop, original), "Input crop must not be mutated"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:

    def test_from_config_creates_correct_classifier(self):
        """from_config builds a classifier matching direct construction."""
        config = {
            "staff_hsv_lower": [100, 80, 50],
            "staff_hsv_upper": [130, 255, 255],
            "staff_threshold": 0.25,
        }
        clf = StaffClassifier.from_config(config)
        assert clf.is_staff(_solid((255, 0, 0))) is True   # blue → staff
        assert clf.is_staff(_solid((0, 0, 255))) is False  # red  → not staff

    def test_from_config_uses_default_threshold_when_absent(self):
        """If staff_threshold is not in config, the default (0.25) is applied."""
        config = {
            "staff_hsv_lower": [100, 80, 50],
            "staff_hsv_upper": [130, 255, 255],
        }
        clf = StaffClassifier.from_config(config)
        # 24% blue → below default threshold → not staff
        assert clf.is_staff(_partial_blue(0.24)) is False
        # 25% blue → at default threshold → staff
        assert clf.is_staff(_partial_blue(0.25)) is True
