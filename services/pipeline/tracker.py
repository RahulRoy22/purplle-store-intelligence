from __future__ import annotations

import numpy as np


class PersonTracker:
    def __init__(self, tracker) -> None:
        self._tracker = tracker
        self._last_track_ids: set[int] = set()

    def update(self, detections: list, frame_bgr: np.ndarray) -> list[dict]:
        raw = self._tracker(detections, frame_bgr)
        result = []
        for t in raw:
            x1, y1, x2, y2 = t["bbox"]
            centroid = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            result.append({
                "track_id": t["track_id"],
                "bbox": t["bbox"],
                "confidence": t["confidence"],
                "centroid": centroid,
            })
        self._last_track_ids = {r["track_id"] for r in result}
        return result

    def get_active_ids(self) -> set[int]:
        return set(self._last_track_ids)
