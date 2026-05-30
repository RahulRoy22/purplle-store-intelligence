import csv
import logging
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
import aiosqlite
from core.config import get_settings
from routers import health, ingest, stores, dashboard

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


_IST = timezone(timedelta(hours=5, minutes=30))

# Date+time format strings tried in order when parsing the organiser CSV.
# The organiser format is typically "DD/MM/YYYY" + "HH:MM:SS", but we cover
# common variants so a format change in a future dataset doesn't re-break us.
_DATETIME_FORMATS = [
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
]


def _parse_organiser_timestamp(date_val: str, time_val: str) -> str:
    """
    Combine order_date + order_time, parse as IST, return as UTC ISO-8601.

    The organiser's POS system records local Indian Standard Time (UTC+5:30).
    We store everything as UTC so unixepoch() comparisons with CV pipeline
    events (which are already UTC) work correctly in the analytics queries.
    """
    date_val = date_val.strip()
    time_val = (time_val or "00:00:00").strip() or "00:00:00"
    combined = f"{date_val} {time_val}"

    for fmt in _DATETIME_FORMATS:
        try:
            naive = datetime.strptime(combined, fmt)
            return naive.replace(tzinfo=_IST).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue

    raise ValueError(f"Cannot parse organiser date/time: {combined!r}")


def _parse_amount(raw: str) -> float | None:
    """Parse amount strings that may be comma-formatted ('1,234.56') or empty."""
    cleaned = raw.strip().replace(",", "") if raw else ""
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_items(raw: str) -> int | None:
    """Parse qty/items_count; treat floats like '2.0' as ints."""
    cleaned = raw.strip() if raw else ""
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


async def load_pos_from_csv(db_path: str, csv_path: str) -> int:
    """
    Load POS transactions from a CSV file into pos_transactions.
    Idempotent: duplicate transaction_ids are silently ignored.
    Returns the number of rows successfully processed.

    Supports two CSV schemas detected automatically from header names:

    Mock schema (data/generated/ — used in development and tests):
        transaction_id, store_id, timestamp, amount_inr,
        items_count, payment_method, terminal_id

    Organiser schema (data/resource/ — real hackathon dataset):
        order_id, store_id, order_date, order_time, total_amount, qty, ...
        Timestamps are in IST (UTC+5:30) and are converted to UTC on ingest.

    Any row that fails to parse is skipped with a WARNING log — one bad row
    never aborts the entire load or crashes the API on startup.
    """
    p = pathlib.Path(csv_path)
    if not p.exists():
        logger.warning("POS CSV not found at %s — skipping load", csv_path)
        return 0

    rows_processed = 0
    rows_skipped = 0

    async with aiosqlite.connect(db_path) as db:
        # utf-8-sig handles both plain UTF-8 and UTF-8-with-BOM (common in
        # Excel-exported CSVs from Indian retail systems)
        with p.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            is_organiser_schema = "order_id" in (reader.fieldnames or [])

            if is_organiser_schema:
                logger.info("POS CSV: detected organiser schema (order_id column)")
            else:
                logger.info("POS CSV: detected mock/standard schema (transaction_id column)")

            for lineno, row in enumerate(reader, start=2):  # line 1 = header
                try:
                    if is_organiser_schema:
                        transaction_id = row["order_id"].strip()
                        store_id       = (row.get("store_id") or "").strip() or settings.store_id
                        timestamp      = _parse_organiser_timestamp(
                                             row["order_date"], row.get("order_time", "")
                                         )
                        amount_inr     = _parse_amount(row.get("total_amount", ""))
                        items_count    = _parse_items(row.get("qty", ""))
                        payment_method = None   # not present in organiser export
                        terminal_id    = None   # not present in organiser export
                    else:
                        transaction_id = row["transaction_id"].strip()
                        store_id       = row["store_id"].strip()
                        timestamp      = row["timestamp"].strip()
                        amount_inr     = _parse_amount(row.get("amount_inr", ""))
                        items_count    = _parse_items(row.get("items_count", ""))
                        payment_method = row.get("payment_method") or None
                        terminal_id    = row.get("terminal_id") or None

                    if not transaction_id:
                        raise ValueError("Empty transaction_id / order_id")

                    await db.execute(
                        """
                        INSERT OR IGNORE INTO pos_transactions
                          (transaction_id, store_id, timestamp, amount_inr,
                           items_count, payment_method, terminal_id)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (transaction_id, store_id, timestamp, amount_inr,
                         items_count, payment_method, terminal_id),
                    )
                    rows_processed += 1

                except Exception as exc:
                    rows_skipped += 1
                    logger.warning(
                        "POS CSV line %d skipped — %s: %s",
                        lineno, type(exc).__name__, exc,
                    )

        await db.commit()

    if rows_skipped:
        logger.warning("POS load complete: %d processed, %d skipped", rows_processed, rows_skipped)
    else:
        logger.info("POS load complete: %d rows processed, 0 skipped", rows_processed)

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
app.include_router(dashboard.router)


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "store-intelligence", "docs": "/docs"}
