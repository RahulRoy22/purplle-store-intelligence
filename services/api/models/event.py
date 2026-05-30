from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# All legal event types in the system
EventType = Literal[
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
]


class EventMetadata(BaseModel):
    """Structured metadata block. Extra keys are allowed for forward-compat."""

    queue_depth: int | None = None
    sku_zone: str | None = None
    session_seq: int = 1

    model_config = {"extra": "allow"}


class EventIn(BaseModel):
    """
    Represents a single inbound CV pipeline event.

    All fields are required unless marked Optional. The schema is intentionally
    strict: unknown fields are ignored, invalid fields reject only that event
    (partial success — the caller is never penalised for a batch with one bad row).
    """

    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: str | None = None
    dwell_ms: int | None = None
    is_staff: bool
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata | dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    @field_validator("event_id")
    @classmethod
    def validate_uuid4(cls, v: str) -> str:
        try:
            parsed = uuid.UUID(v)
            # Accept any UUID version for real data compatibility;
            # version=4 is preferred but not enforced at this layer.
            _ = parsed
        except (ValueError, AttributeError):
            raise ValueError(f"event_id must be a valid UUID, got: {v!r}")
        return v

    @field_validator("timestamp")
    @classmethod
    def ensure_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "timestamp must be timezone-aware (include UTC offset, e.g. +00:00)"
            )
        return v


class RejectedEvent(BaseModel):
    event_id: str | None
    reason: str


class IngestResponse(BaseModel):
    trace_id: str
    accepted: int
    duplicate: int
    rejected: list[RejectedEvent]
    latency_ms: float
