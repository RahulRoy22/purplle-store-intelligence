"""
state_machine.py — Per-track state machine that converts ByteTrack output
into EventIn payloads ready for POST /events/ingest.

Design rules
------------
* Staff events are EMITTED (not suppressed) — filtering is the API's job.
* Each new track_id receives a fresh UUID visitor_id.
* When the caller provides visitor_id (Re-ID match from reid.py), the event
  type is REENTRY instead of ENTRY, and the supplied visitor_id is used.
* ZONE_DWELL and ZONE_EXIT are emitted together when a track leaves a zone;
  dwell_ms spans the full time in zone (zone_enter_ts → current frame_ts).
* BILLING_QUEUE_JOIN is emitted when entering zone_billing; metadata
  includes queue_depth = the number of visitors currently in zone_billing
  (including the arriving visitor).
* BILLING_QUEUE_ABANDON is emitted when leaving zone_billing, regardless
  of dwell time — POS correlation happens in the analytics layer, not here.
* flush_exits() emits EXIT for every tracked visitor absent from the
  supplied active_track_ids set and removes them from internal state.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class _TrackInfo:
    """Internal state for one ByteTrack track."""
    visitor_id: str
    is_staff: bool
    confidence: float
    current_zone: Optional[str] = None
    zone_enter_ts: Optional[datetime] = None


class TrackStateMachine:
    """
    Stateful converter: (track_id, zone_id, frame metadata) → EventIn dicts.

    Parameters
    ----------
    store_id  : str  — forwarded to every emitted event
    camera_id : str  — forwarded to every emitted event
    """

    _BILLING_ZONE = "zone_billing"

    def __init__(self, store_id: str, camera_id: str) -> None:
        self.store_id = store_id
        self.camera_id = camera_id
        self._tracks: dict[int, _TrackInfo] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_track(
        self,
        track_id: int,
        zone_id: Optional[str],
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
        visitor_id: Optional[str] = None,
    ) -> list[dict]:
        """
        Process one tracker detection for the current frame.

        Returns a (possibly empty) list of event dicts to forward to
        the ingest API.  Call once per tracked bounding box per frame.

        Parameters
        ----------
        track_id   : ByteTrack's numeric ID for this track
        zone_id    : Zone the centroid falls in, or None (store floor / exit)
        is_staff   : Output of StaffClassifier for this crop
        confidence : YOLO detection confidence (0–1)
        frame_ts   : Wall-clock timestamp of the current frame (timezone-aware)
        visitor_id : Provided by reid.py when a returning visitor is recognised.
                     If given, emits REENTRY instead of ENTRY and reuses the ID.
        """
        if track_id not in self._tracks:
            return self._on_new_track(track_id, zone_id, is_staff, confidence, frame_ts, visitor_id)
        return self._on_existing_track(track_id, zone_id, is_staff, confidence, frame_ts)

    def flush_exits(self, active_track_ids: set[int], frame_ts: datetime) -> list[dict]:
        """
        Emit EXIT for every tracked visitor not present in *active_track_ids*,
        then remove them from internal state.

        Call once per frame after processing all detections, passing the set
        of track_ids that ByteTrack reported as still active.
        """
        events: list[dict] = []
        gone = [tid for tid in list(self._tracks) if tid not in active_track_ids]
        for tid in gone:
            info = self._tracks.pop(tid)
            events.append(self._evt(
                info.visitor_id, "EXIT",
                zone_id=info.current_zone,
                dwell_ms=None,
                is_staff=info.is_staff,
                confidence=info.confidence,
                frame_ts=frame_ts,
            ))
        return events

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _on_new_track(
        self,
        track_id: int,
        zone_id: Optional[str],
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
        visitor_id: Optional[str],
    ) -> list[dict]:
        vid = visitor_id if visitor_id else str(uuid.uuid4())
        info = _TrackInfo(visitor_id=vid, is_staff=is_staff, confidence=confidence)
        self._tracks[track_id] = info

        events: list[dict] = []
        entry_type = "REENTRY" if visitor_id else "ENTRY"
        events.append(self._evt(vid, entry_type, zone_id, None, is_staff, confidence, frame_ts))

        if zone_id is not None:
            info.current_zone = zone_id
            info.zone_enter_ts = frame_ts
            events.extend(self._enter_zone_events(info, zone_id, is_staff, confidence, frame_ts))

        return events

    def _on_existing_track(
        self,
        track_id: int,
        zone_id: Optional[str],
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
    ) -> list[dict]:
        info = self._tracks[track_id]
        info.is_staff = is_staff
        info.confidence = confidence
        prev_zone = info.current_zone

        if prev_zone == zone_id:
            # Still in same zone (or still on store floor) — nothing to emit
            return []

        events: list[dict] = []

        # --- Leave previous zone ---
        if prev_zone is not None:
            dwell_ms = self._elapsed_ms(info.zone_enter_ts, frame_ts)
            events.append(self._evt(info.visitor_id, "ZONE_DWELL", prev_zone, dwell_ms, is_staff, confidence, frame_ts))
            events.append(self._evt(info.visitor_id, "ZONE_EXIT",  prev_zone, None,     is_staff, confidence, frame_ts))
            if prev_zone == self._BILLING_ZONE:
                events.append(self._evt(info.visitor_id, "BILLING_QUEUE_ABANDON", prev_zone, None, is_staff, confidence, frame_ts))

        # --- Enter new zone ---
        if zone_id is not None:
            info.current_zone = zone_id
            info.zone_enter_ts = frame_ts
            events.extend(self._enter_zone_events(info, zone_id, is_staff, confidence, frame_ts))
        else:
            info.current_zone = None
            info.zone_enter_ts = None

        return events

    def _enter_zone_events(
        self,
        info: _TrackInfo,
        zone_id: str,
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
    ) -> list[dict]:
        """Return ZONE_ENTER (and BILLING_QUEUE_JOIN if applicable)."""
        events: list[dict] = [
            self._evt(info.visitor_id, "ZONE_ENTER", zone_id, None, is_staff, confidence, frame_ts)
        ]
        if zone_id == self._BILLING_ZONE:
            queue_depth = self._count_billing_visitors()
            meta = {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1}
            events.append(self._evt(
                info.visitor_id, "BILLING_QUEUE_JOIN", zone_id, None, is_staff, confidence, frame_ts, meta
            ))
        return events

    def _count_billing_visitors(self) -> int:
        """Number of visitors currently in zone_billing (including arrivals already registered)."""
        return sum(1 for info in self._tracks.values() if info.current_zone == self._BILLING_ZONE)

    @staticmethod
    def _elapsed_ms(start: Optional[datetime], end: datetime) -> int:
        if start is None:
            return 0
        return int((end - start).total_seconds() * 1000)

    def _evt(
        self,
        visitor_id: str,
        event_type: str,
        zone_id: Optional[str],
        dwell_ms: Optional[int],
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
        metadata: Optional[dict] = None,
    ) -> dict:
        return {
            "event_id":   str(uuid.uuid4()),
            "store_id":   self.store_id,
            "camera_id":  self.camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp":  frame_ts.isoformat(),
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": confidence,
            "metadata":   metadata or {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        }
