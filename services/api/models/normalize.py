"""
models/normalize.py — Dual-schema input normalization for /events/ingest.

Why this exists
---------------
The challenge ships TWO event shapes that both legitimately describe the same
behaviour, and a robust ingest must accept either without rejection:

1. **Canonical schema** (PDF page-5 "Required Output Schema", and what our own
   detection pipeline emits): UPPERCASE ``event_type``, ``event_id`` (uuid),
   ``visitor_id``, ``store_id``, ``timestamp``, ``zone_id``, ``is_staff``,
   ``confidence``, ``metadata``.

2. **Shipped detection schema** (the organiser's ``sample_events.jsonl``):
   lowercase ``event_type`` (``entry``/``zone_entered``/``queue_completed`` …),
   ``id_token``/``track_id`` instead of ``visitor_id``, ``store_code``/``store_id``,
   ``event_timestamp``/``event_time``/``queue_join_ts`` for the time, naive
   (tz-less) timestamps, no ``event_id`` on most rows, no ``confidence``, and a
   set of rich fields (``gender_pred``, ``age_pred``, ``age_bucket``,
   ``group_id``, ``group_size``, ``zone_type``, ``is_revenue_zone``,
   ``zone_hotspot_x/y``, ``wait_seconds``, ``queue_position_at_join``,
   ``abandoned``).

``normalize_event`` maps shape 2 onto the canonical shape that ``EventIn``
validates and that the analytics SQL already understands. It is a **pure**
function: no I/O, never raises (an unrecognised row is returned unchanged so the
existing per-event validation/partial-success path can reject just that row),
and it preserves idempotency by deriving a *deterministic* ``event_id`` for rows
that ship without one.

Crucially, it canonicalises billing zones to ``zone_billing`` because the
analytics queries (conversion, funnel stage-4, queue depth, anomalies) match on
that literal zone_id. Other zones are slugged consistently so funnel/heatmap
grouping is stable.
"""
from __future__ import annotations

import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

# --- Built-in defaults (authoritative fallback if schema_map.yaml is absent) --
# These mirror schema_map.yaml exactly so behaviour is identical whether or not
# the config file (or PyYAML) is present — the API never hard-depends on either.
_DEFAULT_TYPE_MAP = {
    "entry": "ENTRY",
    "exit": "EXIT",
    "zone_entered": "ZONE_ENTER",
    "zone_exited": "ZONE_EXIT",
    "zone_dwell": "ZONE_DWELL",
    "queue_completed": "BILLING_QUEUE_JOIN",
    "queue_abandoned": "BILLING_QUEUE_ABANDON",
    "reentry": "REENTRY",
}
_DEFAULT_VISITOR_FIELDS = ["id_token", "visitor_id"]
_DEFAULT_TIMESTAMP_FIELDS = ["timestamp", "event_timestamp", "event_time", "queue_join_ts"]
_DEFAULT_STORE_FIELDS = ["store_id", "store_code"]
_DEFAULT_METADATA_CARRY = [
    "gender", "gender_pred", "age", "age_pred", "age_bucket",
    "group_id", "group_size", "is_face_hidden", "is_revenue_zone",
    "zone_name", "zone_type", "zone_hotspot_x", "zone_hotspot_y",
    "wait_seconds", "queue_position_at_join", "queue_served_ts",
    "queue_exit_ts", "abandoned", "camera_id",
]
_DEFAULT_CONFIDENCE_VALUE = 0.9

_CONFIG_PATH = pathlib.Path(__file__).with_name("schema_map.yaml")


def _load_event_config() -> dict[str, Any]:
    """
    Load the `events:` section of schema_map.yaml, falling back to built-in
    defaults for any missing key (and entirely if the file / PyYAML is absent).
    New schemas are onboarded by editing the YAML, not this module.
    """
    cfg: dict[str, Any] = {}
    try:
        import yaml  # optional dependency — fall back cleanly if unavailable
        if _CONFIG_PATH.exists():
            cfg = (yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("events", {}) or {}
    except Exception:
        cfg = {}
    return {
        "type_map": cfg.get("type_map") or _DEFAULT_TYPE_MAP,
        "visitor_id_fields": cfg.get("visitor_id_fields") or _DEFAULT_VISITOR_FIELDS,
        "timestamp_fields": cfg.get("timestamp_fields") or _DEFAULT_TIMESTAMP_FIELDS,
        "store_id_fields": cfg.get("store_id_fields") or _DEFAULT_STORE_FIELDS,
        "metadata_carry": cfg.get("metadata_carry") or _DEFAULT_METADATA_CARRY,
        "default_confidence": cfg.get("default_confidence", _DEFAULT_CONFIDENCE_VALUE),
    }


_CFG = _load_event_config()
_TYPE_MAP = _CFG["type_map"]
_VISITOR_FIELDS = _CFG["visitor_id_fields"]
_TIMESTAMP_FIELDS = _CFG["timestamp_fields"]
_STORE_FIELDS = _CFG["store_id_fields"]
_METADATA_CARRY = tuple(_CFG["metadata_carry"])
_DEFAULT_CONFIDENCE = _CFG["default_confidence"]

# Canonical (already-normalised) event types. If a row already uses one of these
# AND carries an event_id + visitor_id, it is treated as canonical and passed
# through untouched.
_CANONICAL_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}

# Stable namespace so uuid5-derived event_ids are reproducible across processes
# and restarts (idempotency: re-POSTing the same shipped row yields a duplicate,
# not a new insert).
_EVENT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "purplle-store-intelligence/events")


def _looks_canonical(raw: dict) -> bool:
    """True if the row is already in our canonical schema (skip normalization)."""
    return (
        raw.get("event_type") in _CANONICAL_TYPES
        and "event_id" in raw
        and "visitor_id" in raw
    )


def _is_shipped(raw: dict) -> bool:
    """Detect the organiser detection schema."""
    et = raw.get("event_type")
    if isinstance(et, str) and et in _TYPE_MAP:
        return True
    # Distinctive shipped-only keys (covers rows whose type we don't map yet).
    return any(k in raw for k in ("id_token", "track_id", "event_time",
                                  "event_timestamp", "queue_event_id"))


def _to_utc_iso(value: Any) -> str | None:
    """Parse an ISO-8601 string (possibly naive) and return tz-aware UTC ISO."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _first_present(raw: dict, fields: list[str]) -> Any:
    """Return the first non-empty value among `fields` (config-driven aliases)."""
    for f in fields:
        v = raw.get(f)
        if v:
            return v
    return None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "unknown"


def _canon_zone(raw: dict, event_type: str) -> str | None:
    """
    Canonicalise a shipped zone into the literal zone_ids the analytics use.

    - Anything billing-related -> 'zone_billing' (conversion / funnel / queue
      SQL match on this exact value).
    - ENTRY/EXIT carry no zone in the canonical schema -> None.
    - Other zones -> 'zone_<slug-of-name>' for stable funnel/heatmap grouping.
    """
    if event_type in ("ENTRY", "EXIT", "REENTRY"):
        return None

    zone_type = str(raw.get("zone_type") or "").upper()
    zone_id = str(raw.get("zone_id") or "")
    zone_name = str(raw.get("zone_name") or "")

    if (
        event_type in ("BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON")
        or zone_type == "BILLING"
        or "BILLING" in zone_id.upper()
        or "billing" in zone_name.lower()
    ):
        return "zone_billing"

    label = zone_name or zone_id
    if not label:
        return None
    slug = _slug(label)
    return slug if slug.startswith("zone_") else f"zone_{slug}"


def _derive_event_id(raw: dict, visitor_id: str, event_type: str,
                     timestamp: str | None, zone_id: str | None) -> str:
    """
    Deterministic event_id for rows that ship without one.

    A real uuid already on the row (queue_event_id, or an existing event_id) is
    reused as-is. Otherwise a uuid5 over the stable identity of the event keeps
    re-ingestion idempotent.
    """
    for key in ("event_id", "queue_event_id"):
        val = raw.get(key)
        if val:
            try:
                return str(uuid.UUID(str(val)))
            except (ValueError, AttributeError):
                pass  # not a uuid — fold it into the derivation below
    seed = "|".join(str(p) for p in (
        raw.get("store_id") or raw.get("store_code") or "",
        visitor_id, event_type, timestamp or "", zone_id or "",
    ))
    return str(uuid.uuid5(_EVENT_NS, seed))


def normalize_event(raw: dict) -> dict:
    """
    Map a single event dict onto the canonical EventIn shape.

    Returns the row unchanged when it is already canonical or when it is not a
    recognisable shipped row (so the caller's validation can reject just that
    row — never the whole batch).
    """
    if not isinstance(raw, dict) or _looks_canonical(raw) or not _is_shipped(raw):
        return raw

    raw_type = str(raw.get("event_type") or "").lower()
    event_type = _TYPE_MAP.get(raw_type)
    if event_type is None:
        return raw  # unknown shipped type — let validation handle it

    # --- visitor_id: configured id fields, else track_<id> ----------------
    visitor_id = _first_present(raw, _VISITOR_FIELDS)
    if not visitor_id and raw.get("track_id") is not None:
        visitor_id = f"track_{raw['track_id']}"
    if not visitor_id:
        return raw

    # --- timestamp: first present of the configured time fields -----------
    timestamp = _to_utc_iso(_first_present(raw, _TIMESTAMP_FIELDS))
    if timestamp is None:
        return raw

    store_id = _first_present(raw, _STORE_FIELDS)
    camera_id = raw.get("camera_id") or ""
    zone_id = _canon_zone(raw, event_type)

    # --- metadata: required keys + carried rich detection fields ----------
    metadata: dict[str, Any] = {
        "queue_depth": None,
        "sku_zone": raw.get("zone_name"),
        "session_seq": 1,
    }
    for key in _METADATA_CARRY:
        if key in raw and raw[key] is not None:
            metadata[key] = raw[key]
    if event_type == "BILLING_QUEUE_JOIN":
        # queue_position_at_join is the depth the visitor saw on joining.
        depth = raw.get("queue_position_at_join")
        if depth is not None:
            metadata["queue_depth"] = depth

    confidence = raw.get("confidence")
    confidence = float(confidence) if confidence is not None else _DEFAULT_CONFIDENCE

    return {
        "event_id": _derive_event_id(raw, str(visitor_id), event_type, timestamp, zone_id),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": str(visitor_id),
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": raw.get("dwell_ms"),
        "is_staff": bool(raw.get("is_staff", False)),
        "confidence": confidence,
        "metadata": metadata,
    }
