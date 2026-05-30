"""
staff_classifier.py — Detects staff members from BGR image crops.

Strategy: convert the crop to HSV colour space and measure what fraction
of pixels fall within the staff-uniform HSV range. If that fraction meets
or exceeds the configurable threshold, the crop is classified as staff.

This approach requires zero model training — only the uniform's HSV range
and a pixel-ratio threshold are needed, both configurable per deployment.

Boundary contract: ``ratio >= threshold`` (inclusive lower bound) so a
partially-occluded staff member who just reaches the threshold is still
flagged correctly.
"""
from __future__ import annotations

import cv2
import numpy as np


class StaffClassifier:
    """
    HSV-range-based staff detector.

    Parameters
    ----------
    hsv_lower : np.ndarray, shape (3,), dtype uint8
        Lower bound of the staff-uniform HSV range (OpenCV convention:
        H ∈ [0, 179], S and V ∈ [0, 255]).
    hsv_upper : np.ndarray, shape (3,), dtype uint8
        Upper bound (inclusive) of the staff-uniform HSV range.
    threshold : float
        Minimum fraction of matching pixels required to classify a crop
        as staff. Default 0.25 (25 %).
    """

    def __init__(
        self,
        hsv_lower: np.ndarray,
        hsv_upper: np.ndarray,
        threshold: float = 0.25,
    ) -> None:
        self.hsv_lower = np.asarray(hsv_lower, dtype=np.uint8)
        self.hsv_upper = np.asarray(hsv_upper, dtype=np.uint8)
        self.threshold = threshold

    def is_staff(self, crop_bgr: np.ndarray) -> bool:
        """
        Return True if *crop_bgr* contains enough uniform-colour pixels.

        Parameters
        ----------
        crop_bgr : np.ndarray
            BGR image crop of a detected person bounding box.
            An empty array (size == 0) returns False without crashing.

        Notes
        -----
        The input array is never modified.
        """
        if crop_bgr is None or crop_bgr.size == 0:
            return False

        hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.hsv_lower, self.hsv_upper)
        ratio = np.count_nonzero(mask) / mask.size
        return bool(ratio >= self.threshold)

    @classmethod
    def from_config(cls, config: dict) -> "StaffClassifier":
        """
        Build a StaffClassifier from a config dict.

        Expected keys:
            staff_hsv_lower   : list[int]  — 3-element HSV lower bound
            staff_hsv_upper   : list[int]  — 3-element HSV upper bound
            staff_threshold   : float      — optional, defaults to 0.25
        """
        return cls(
            hsv_lower=np.array(config["staff_hsv_lower"], dtype=np.uint8),
            hsv_upper=np.array(config["staff_hsv_upper"], dtype=np.uint8),
            threshold=float(config.get("staff_threshold", 0.25)),
        )
