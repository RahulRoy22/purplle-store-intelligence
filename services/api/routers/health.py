from fastapi import APIRouter
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel
import aiosqlite
from core.config import get_settings

router = APIRouter()


class StoreFeed(BaseModel):
    store_id: str
    last_event_at: str | None
    stale_feed: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["connected", "error"]
    stale_feed: bool                 # global: True if ANY store's feed is stale
    last_event_at: str | None        # global: most-recent event across all stores
    stores: list[StoreFeed]          # per-store last event + staleness
    checked_at: str


def _is_stale(last_event_at: str, threshold_minutes: int) -> bool:
    age_minutes = (
        datetime.now(timezone.utc) - datetime.fromisoformat(last_event_at)
    ).total_seconds() / 60
    return age_minutes > threshold_minutes


@router.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check() -> HealthResponse:
    settings = get_settings()
    checked_at = datetime.now(timezone.utc).isoformat()
    db_status: Literal["connected", "error"] = "connected"
    last_event_at: str | None = None
    stale_feed = False
    stores: list[StoreFeed] = []

    try:
        async with aiosqlite.connect(settings.db_path) as db:
            # Per-store last event timestamp
            async with db.execute(
                "SELECT store_id, MAX(timestamp) FROM events GROUP BY store_id"
            ) as cursor:
                rows = await cursor.fetchall()

            for store_id, store_last in rows:
                if not store_last:
                    continue
                store_stale = _is_stale(store_last, settings.stale_feed_threshold_minutes)
                stores.append(StoreFeed(
                    store_id=store_id,
                    last_event_at=store_last,
                    stale_feed=store_stale,
                ))
                # Global last_event_at = newest across all stores
                if last_event_at is None or store_last > last_event_at:
                    last_event_at = store_last

            # Global stale_feed: stale if the newest event anywhere is stale.
            if last_event_at is not None:
                stale_feed = _is_stale(last_event_at, settings.stale_feed_threshold_minutes)
    except Exception:
        db_status = "error"

    overall = "ok" if db_status == "connected" and not stale_feed else "degraded"
    return HealthResponse(
        status=overall,
        db=db_status,
        stale_feed=stale_feed,
        last_event_at=last_event_at,
        stores=stores,
        checked_at=checked_at,
    )
