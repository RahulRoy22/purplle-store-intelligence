from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


class EventBuilder:
    """
    Converts raw state-machine event dicts into fully-stamped dicts that
    pass EventIn Pydantic validation and are ready to POST to /events/ingest.
    """

    _DEFAULT_METADATA: dict[str, Any] = {
        "queue_depth": None,
        "sku_zone": None,
        "session_seq": 1,
    }

    def __init__(
        self,
        store_id: str,
        zone_camera_map: dict[str, str],
        default_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._store_id = store_id
        self._zone_camera_map = zone_camera_map
        self._default_metadata = default_metadata or self._DEFAULT_METADATA

    def build(self, raw_events: list[dict]) -> list[dict]:
        return [self._stamp(raw) for raw in raw_events]

    def _stamp(self, raw: dict) -> dict:
        zone_id: str | None = raw.get("zone_id")

        # camera_id: look up from zone map; fall back to empty string for None zones
        camera_id: str = self._zone_camera_map.get(zone_id, "") if zone_id is not None else ""

        # timestamp: preserve the original tz-aware datetime, serialised as ISO-8601
        ts: datetime = raw["timestamp"]
        if ts.tzinfo is None:
            # Normalise naive datetimes to UTC rather than crashing
            ts = ts.replace(tzinfo=timezone.utc)
        timestamp_str: str = ts.isoformat()

        # is_staff: explicit Python bool cast — guards against np.bool_ contamination
        is_staff: bool = bool(raw.get("is_staff", False))

        # metadata: merge defaults so all required keys are always present
        meta: dict[str, Any] = {**self._default_metadata, **raw.get("metadata", {})}

        return {
            "event_id":   str(uuid.uuid4()),
            "store_id":   self._store_id,
            "camera_id":  camera_id,
            "visitor_id": raw["visitor_id"],
            "event_type": raw["event_type"],
            "timestamp":  timestamp_str,
            "zone_id":    zone_id,
            "dwell_ms":   raw.get("dwell_ms"),
            "is_staff":   is_staff,
            "confidence": float(raw["confidence"]),
            "metadata":   meta,
        }
