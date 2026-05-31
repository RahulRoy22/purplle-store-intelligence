from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

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


class SharedRegistry:
    """
    Cross-camera visitor embedding registry backed by SQLite.

    Each camera process holds an in-memory dict loaded from DB on startup and
    refreshed every 60 s to pick up registrations from sibling camera processes.
    New visitors are written to DB immediately so other cameras can see them.
    Cosine similarity on L2-normalised vectors reduces to a dot product.
    """

    _REFRESH_INTERVAL = 60  # seconds

    def __init__(self, store_id: str, db_path: Optional[str] = None) -> None:
        self._store_id = store_id
        self._db_path = db_path or os.environ.get("DB_PATH", "/data/store_intelligence.db")
        self._lock = threading.Lock()
        self._cache: dict[str, np.ndarray] = {}
        self._last_refresh: float = 0.0
        self._load_from_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, embedding: np.ndarray, threshold: float) -> Optional[str]:
        """Return visitor_id of the closest match if similarity >= threshold."""
        self._maybe_refresh()
        with self._lock:
            if not self._cache:
                return None
            best_id = max(
                self._cache,
                key=lambda vid: float(np.dot(embedding, self._cache[vid])),
            )
            best_sim = float(np.dot(embedding, self._cache[best_id]))
            return best_id if best_sim >= threshold else None

    def register(self, visitor_id: str, embedding: np.ndarray) -> None:
        """Store embedding in memory and persist to DB."""
        with self._lock:
            self._cache[visitor_id] = embedding
        self._write_to_db(visitor_id, embedding)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_refresh(self) -> None:
        if time.monotonic() - self._last_refresh >= self._REFRESH_INTERVAL:
            self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            try:
                rows = con.execute(
                    "SELECT visitor_id, embedding FROM visitor_embeddings WHERE store_id = ?",
                    (self._store_id,),
                ).fetchall()
                loaded: dict[str, np.ndarray] = {}
                for visitor_id, blob in rows:
                    arr = np.frombuffer(blob, dtype=np.float32).copy()
                    loaded[visitor_id] = arr
                with self._lock:
                    self._cache.update(loaded)
                self._last_refresh = time.monotonic()
            finally:
                con.close()
        except Exception:
            pass  # DB may not exist yet on first startup

    def _write_to_db(self, visitor_id: str, embedding: np.ndarray) -> None:
        try:
            con = sqlite3.connect(self._db_path, timeout=5)
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO visitor_embeddings
                      (visitor_id, store_id, embedding, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        visitor_id,
                        self._store_id,
                        embedding.astype(np.float32).tobytes(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass  # non-fatal: in-memory cache still works for this process
