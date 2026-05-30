"""
routers/ingest.py -- POST /events/ingest

Key guarantees:
- Idempotency  : duplicate event_ids are silently skipped (INSERT OR IGNORE)
- Partial OK   : validation failures per-event; good events always land
- Structured503: DB failures return {"error": "database_unavailable", "trace_id": ...}
                 — never a Python traceback
- Structured log: every request emits a single JSON line with trace_id,
                  store_id, batch_size, accepted, duplicate, rejected, latency_ms
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from core.config import get_settings
from db.events import batch_upsert
from models.event import EventIn, IngestResponse, RejectedEvent

router = APIRouter()
logger = logging.getLogger("ingest")

MAX_BATCH_SIZE = 500


class IngestRequest(BaseModel):
    """
    Wrapping in an object (rather than a bare list) keeps the schema
    extensible — future fields like `source_pipeline_version` slot in here
    without breaking existing callers.
    """

    events: list[dict[str, Any]]


@router.post(
    "/events/ingest",
    response_model=IngestResponse,
    tags=["ingestion"],
    summary="Batch-ingest CV pipeline events (idempotent)",
)
async def ingest_events(payload: IngestRequest) -> IngestResponse:
    trace_id = str(uuid.uuid4())
    settings = get_settings()
    t0 = time.monotonic()

    # --- Batch size gate ---------------------------------------------------
    if len(payload.events) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=422,
            detail=f"Batch size {len(payload.events)} exceeds the maximum of {MAX_BATCH_SIZE}.",
        )

    # --- Per-event validation (partial success pattern) --------------------
    valid_events: list[EventIn] = []
    rejected: list[RejectedEvent] = []

    for raw in payload.events:
        try:
            event = EventIn.model_validate(raw)
            valid_events.append(event)
        except ValidationError as exc:
            # Surface the first validation error message; keep it human-readable
            first_error = exc.errors(include_url=False)[0]
            reason = f"{'.'.join(str(l) for l in first_error['loc'])}: {first_error['msg']}"
            rejected.append(
                RejectedEvent(
                    event_id=raw.get("event_id"),
                    reason=reason,
                )
            )

    # --- DB write (all-or-nothing for valid events) ------------------------
    inserted = 0
    duplicate = 0

    if valid_events:
        try:
            result = await batch_upsert(settings.db_path, valid_events)
            inserted = result["inserted"]
            duplicate = result["duplicate"]
        except Exception as exc:
            logger.error(
                json.dumps({
                    "event": "ingest_db_error",
                    "trace_id": trace_id,
                    "error": str(exc),
                })
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "database_unavailable",
                    "message": (
                        "Event ingestion failed due to a transient database error. "
                        "Retry with exponential backoff."
                    ),
                    "trace_id": trace_id,
                },
            )

    latency_ms = round((time.monotonic() - t0) * 1000, 2)

    # --- Structured audit log ----------------------------------------------
    store_id = valid_events[0].store_id if valid_events else None
    logger.info(
        json.dumps({
            "event": "ingest_complete",
            "trace_id": trace_id,
            "store_id": store_id,
            "batch_size": len(payload.events),
            "accepted": inserted,
            "duplicate": duplicate,
            "rejected": len(rejected),
            "latency_ms": latency_ms,
        })
    )

    return IngestResponse(
        trace_id=trace_id,
        accepted=inserted,
        duplicate=duplicate,
        rejected=rejected,
        latency_ms=latency_ms,
    )
