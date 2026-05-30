from __future__ import annotations

import numpy as np


class PersonDetector:
    def __init__(self, model, conf_threshold: float = 0.5) -> None:
        self._model = model
        self._conf_threshold = conf_threshold

    def detect(self, frame_bgr: np.ndarray) -> list[dict]:
        raw = self._model(frame_bgr)
        return [
            {"bbox": d["bbox"], "confidence": d["confidence"]}
            for d in raw
            if d["class_id"] == 0 and d["confidence"] >= self._conf_threshold
        ]
