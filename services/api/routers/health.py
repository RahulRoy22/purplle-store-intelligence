from fastapi import APIRouter
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel
import aiosqlite
from core.config import get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["connected", "error"]
    stale_feed: bool
    last_event_at: str | None
    checked_at: str


@router.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check() -> HealthResponse:
    settings = get_settings()
    checked_at = datetime.now(timezone.utc).isoformat()
    db_status: Literal["connected", "error"] = "connected"
    last_event_at: str | None = None
    stale_feed = False

    try:
        async with aiosqlite.connect(settings.db_path) as db:
            async with db.execute(
                "SELECT MAX(timestamp) FROM events"
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    last_event_at = row[0]
                    last_dt = datetime.fromisoformat(last_event_at)
                    age_minutes = (
                        datetime.now(timezone.utc) - last_dt
                    ).total_seconds() / 60
                    stale_feed = age_minutes > settings.stale_feed_threshold_minutes
    except Exception:
        db_status = "error"

    overall = "ok" if db_status == "connected" and not stale_feed else "degraded"
    return HealthResponse(
        status=overall,
        db=db_status,
        stale_feed=stale_feed,
        last_event_at=last_event_at,
        checked_at=checked_at,
    )
