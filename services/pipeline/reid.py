from __future__ import annotations

import numpy as np


class ReIdentifier:
    def __init__(self, model, threshold: float = 0.85) -> None:
        self._model = model
        self._threshold = threshold
        self._registry: dict[str, np.ndarray] = {}

    def extract_embedding(self, crop_bgr: np.ndarray) -> np.ndarray:
        raw = self._model(crop_bgr)
        norm = np.linalg.norm(raw)
        if norm == 0:
            return raw.astype(np.float32)
        return (raw / norm).astype(np.float32)

    def register(self, visitor_id: str, embedding: np.ndarray) -> None:
        self._registry[visitor_id] = embedding

    def identify(self, embedding: np.ndarray) -> str | None:
        if not self._registry:
            return None
        best_id = max(self._registry, key=lambda vid: float(np.dot(embedding, self._registry[vid])))
        best_sim = float(np.dot(embedding, self._registry[best_id]))
        return best_id if best_sim >= self._threshold else None

    def forget(self, visitor_id: str) -> None:
        self._registry.pop(visitor_id, None)
