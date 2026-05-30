import csv
import logging
import pathlib
from contextlib import asynccontextmanager
from fastapi import FastAPI
import aiosqlite
from core.config import get_settings
from routers import health, ingest, stores

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

CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id  TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    amount_inr      REAL,
    items_count     INTEGER,
    payment_method  TEXT,
    terminal_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_store_ts
    ON pos_transactions(store_id, timestamp);
"""


async def init_db(db_path: str) -> None:
    """Create DB schema. Called by lifespan (production) and test fixtures."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(DDL)
        await db.commit()


async def load_pos_from_csv(db_path: str, csv_path: str) -> int:
    """
    Load POS transactions from a CSV file into pos_transactions.
    Idempotent: duplicate transaction_ids are silently ignored.
    Returns the number of rows processed (not necessarily inserted).
    """
    p = pathlib.Path(csv_path)
    if not p.exists():
        logger.warning("POS CSV not found at %s — skipping load", csv_path)
        return 0

    rows_processed = 0
    async with aiosqlite.connect(db_path) as db:
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                await db.execute(
                    """
                    INSERT OR IGNORE INTO pos_transactions
                      (transaction_id, store_id, timestamp, amount_inr,
                       items_count, payment_method, terminal_id)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        row["transaction_id"],
                        row["store_id"],
                        row["timestamp"],
                        float(row["amount_inr"]),
                        int(row["items_count"]),
                        row["payment_method"],
                        row["terminal_id"],
                    ),
                )
                rows_processed += 1
        await db.commit()
    return rows_processed


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — ensuring DB schema")
    await init_db(settings.db_path)
    logger.info("DB schema ready at %s", settings.db_path)
    if settings.pos_csv_path:
        n = await load_pos_from_csv(settings.db_path, settings.pos_csv_path)
        logger.info("Loaded %d POS rows from %s", n, settings.pos_csv_path)
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="Purplle Store Intelligence API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(stores.router)


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "store-intelligence", "docs": "/docs"}
