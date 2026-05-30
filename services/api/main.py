import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
import aiosqlite
from core.config import get_settings
from routers import health

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger("api")

DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL,
    camera_id     TEXT NOT NULL,
    visitor_id    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    zone_id       TEXT,
    dwell_ms      INTEGER,
    is_staff      INTEGER NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL,
    metadata      TEXT,
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_store_ts
    ON events(store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_visitor
    ON events(visitor_id, timestamp);
"""


async def init_db(db_path: str) -> None:
    """Create DB schema. Called by lifespan (production) and test fixtures."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(DDL)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — ensuring DB schema")
    await init_db(settings.db_path)
    logger.info("DB schema ready at %s", settings.db_path)
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="Purplle Store Intelligence API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health.router)


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "store-intelligence", "docs": "/docs"}
