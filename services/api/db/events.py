"""
db/events.py — Low-level DB helpers for the events table.

Design notes:
- INSERT OR IGNORE enforces idempotency at the DB constraint level, not
  in application logic. This is safe under concurrent writers because
  SQLite serialises writes at the database level.
- We query for existing event_ids BEFORE the batch insert so we can
  return an accurate "duplicate" count to the caller without relying on
  cursor.rowcount (which behaves differently across aiosqlite versions).
- metadata is stored as a JSON string. Reads back as TEXT; callers must
  json.loads() it.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from models.event import EventIn


async def batch_upsert(db_path: str, events: list[EventIn]) -> dict[str, int]:
    """
    Idempotently insert a batch of events.

    Returns
    -------
    {"inserted": int, "duplicate": int}
        inserted  — events that were new and written to the DB
        duplicate — events whose event_id already existed (silently ignored)
    """
    if not events:
        return {"inserted": 0, "duplicate": 0}

    event_ids = [ev.event_id for ev in events]

    async with aiosqlite.connect(db_path) as db:
        # 1. Find which event_ids already exist in a single query
        placeholders = ",".join("?" * len(event_ids))
        async with db.execute(
            f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        ) as cursor:
            existing_ids: set[str] = {row[0] for row in await cursor.fetchall()}

        # 2. Build rows and batch-insert with IGNORE on conflict
        rows: list[tuple[Any, ...]] = []
        for ev in events:
            meta = ev.metadata
            meta_json = (
                json.dumps(meta)
                if isinstance(meta, dict)
                else json.dumps(meta.model_dump())
            )
            rows.append((
                ev.event_id,
                ev.store_id,
                ev.camera_id,
                ev.visitor_id,
                ev.event_type,
                ev.timestamp.isoformat(),
                ev.zone_id,
                ev.dwell_ms,
                int(ev.is_staff),
                ev.confidence,
                meta_json,
            ))

        await db.executemany(
            """INSERT OR IGNORE INTO events
               (event_id, store_id, camera_id, visitor_id, event_type,
                timestamp, zone_id, dwell_ms, is_staff, confidence, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        await db.commit()

    duplicate = len(existing_ids)
    inserted = len(events) - duplicate
    return {"inserted": inserted, "duplicate": duplicate}
