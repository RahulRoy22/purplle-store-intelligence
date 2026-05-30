"""
db/analytics.py — Pure SQL analytics queries for the Store Intelligence API.

Design rules enforced here:
  - Every query filters is_staff = 0 (staff excluded at read time)
  - POS correlation uses unixepoch() exclusively (no Python datetime arithmetic)
  - REENTRY deduplication is implicit: COUNT(DISTINCT visitor_id) over
    event_type IN ('ENTRY','REENTRY') counts a returning visitor once
  - pos_transactions JOIN failures are caught so the API stays up even
    before POS data is loaded (zero-value fallback)
"""
from __future__ import annotations

import json
import aiosqlite
from datetime import datetime, timezone


async def get_metrics(db_path: str, store_id: str) -> dict:
    """Aggregate KPIs for a store. All metrics exclude staff events."""
    async with aiosqlite.connect(db_path) as db:

        # Unique visitors — REENTRY deduped via COUNT(DISTINCT visitor_id)
        async with db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = ? AND is_staff = 0
              AND event_type IN ('ENTRY', 'REENTRY')
            """,
            (store_id,),
        ) as cur:
            unique_visitors: int = (await cur.fetchone())[0] or 0

        # Converted visitors: were in zone_billing within 300 s of a POS transaction
        converted = 0
        try:
            async with db.execute(
                """
                SELECT COUNT(DISTINCT e.visitor_id)
                FROM   events           e
                JOIN   pos_transactions p ON p.store_id = e.store_id
                WHERE  e.store_id  = ?
                  AND  e.is_staff  = 0
                  AND  e.zone_id   = 'zone_billing'
                  AND  unixepoch(p.timestamp) >= unixepoch(e.timestamp)
                  AND  unixepoch(p.timestamp) -  unixepoch(e.timestamp) <= 300
                """,
                (store_id,),
            ) as cur:
                converted = (await cur.fetchone())[0] or 0
        except Exception:
            # pos_transactions table may not exist yet — return zero rather than 500
            converted = 0

        conversion_rate = (converted / unique_visitors) if unique_visitors > 0 else 0.0

        # Average dwell (customers only, ZONE_DWELL events)
        async with db.execute(
            """
            SELECT AVG(dwell_ms)
            FROM   events
            WHERE  store_id = ? AND is_staff = 0 AND dwell_ms IS NOT NULL
            """,
            (store_id,),
        ) as cur:
            avg_dwell_raw = (await cur.fetchone())[0]

        # Billing abandonment rate
        async with db.execute(
            """
            SELECT
              SUM(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END),
              SUM(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 ELSE 0 END)
            FROM  events
            WHERE store_id = ? AND is_staff = 0
            """,
            (store_id,),
        ) as cur:
            row = await cur.fetchone()
            abandons, joins = (row[0] or 0), (row[1] or 0)

        total_billing = abandons + joins
        abandonment_rate = (abandons / total_billing) if total_billing > 0 else 0.0

    return {
        "store_id": store_id,
        "unique_visitors": unique_visitors,
        "conversion_rate": round(conversion_rate, 4),
        "avg_dwell_ms": round(avg_dwell_raw, 2) if avg_dwell_raw is not None else None,
        "billing_abandonment_rate": round(abandonment_rate, 4),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_funnel(db_path: str, store_id: str) -> dict:
    """Visitor funnel: Entry → Zone Visit → Billing Queue → Purchase."""
    async with aiosqlite.connect(db_path) as db:

        # Stage 1: Entry (REENTRY deduped)
        async with db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id)
            FROM   events
            WHERE  store_id = ? AND is_staff = 0
              AND  event_type IN ('ENTRY', 'REENTRY')
            """,
            (store_id,),
        ) as cur:
            entry_count: int = (await cur.fetchone())[0] or 0

        # Stage 2: Visited a floor zone (any ZONE_ENTER outside the entry zone)
        async with db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id)
            FROM   events
            WHERE  store_id  = ? AND is_staff = 0
              AND  event_type = 'ZONE_ENTER'
              AND  zone_id   != 'zone_entry'
            """,
            (store_id,),
        ) as cur:
            zone_visit_count: int = (await cur.fetchone())[0] or 0

        # Stage 3: Joined billing queue
        async with db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id)
            FROM   events
            WHERE  store_id  = ? AND is_staff = 0
              AND  event_type = 'BILLING_QUEUE_JOIN'
            """,
            (store_id,),
        ) as cur:
            billing_count: int = (await cur.fetchone())[0] or 0

        # Stage 4: Purchase (5-minute POS window)
        purchase_count = 0
        try:
            async with db.execute(
                """
                SELECT COUNT(DISTINCT e.visitor_id)
                FROM   events           e
                JOIN   pos_transactions p ON p.store_id = e.store_id
                WHERE  e.store_id  = ?
                  AND  e.is_staff  = 0
                  AND  e.zone_id   = 'zone_billing'
                  AND  unixepoch(p.timestamp) >= unixepoch(e.timestamp)
                  AND  unixepoch(p.timestamp) -  unixepoch(e.timestamp) <= 300
                """,
                (store_id,),
            ) as cur:
                purchase_count = (await cur.fetchone())[0] or 0
        except Exception:
            purchase_count = 0

    def _pct(n: int) -> float:
        if entry_count == 0:
            return 0.0
        return round(n / entry_count * 100, 2)

    stages = [
        {"stage": "entry",         "visitors": entry_count,      "pct_of_entry": _pct(entry_count)},
        {"stage": "zone_visit",    "visitors": zone_visit_count, "pct_of_entry": _pct(zone_visit_count)},
        {"stage": "billing_queue", "visitors": billing_count,    "pct_of_entry": _pct(billing_count)},
        {"stage": "purchase",      "visitors": purchase_count,   "pct_of_entry": _pct(purchase_count)},
    ]

    return {
        "store_id": store_id,
        "stages": stages,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_heatmap(db_path: str, store_id: str) -> dict:
    """
    Zone visit frequency and average dwell, normalised to a 0–100 heat score.

    data_confidence is "high" when ≥ 20 unique visitor sessions have been
    recorded; "low" otherwise — a flag for the consumer that results may
    not be statistically representative.
    """
    async with aiosqlite.connect(db_path) as db:

        # Count unique visitor sessions (ENTRY + REENTRY, staff excluded)
        async with db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id)
            FROM   events
            WHERE  store_id = ? AND is_staff = 0
              AND  event_type IN ('ENTRY', 'REENTRY')
            """,
            (store_id,),
        ) as cur:
            session_count: int = (await cur.fetchone())[0] or 0

        async with db.execute(
            """
            SELECT
              zone_id,
              SUM(CASE WHEN event_type = 'ZONE_ENTER' THEN 1 ELSE 0 END)           AS visit_count,
              AVG(CASE WHEN event_type = 'ZONE_DWELL' AND dwell_ms IS NOT NULL
                       THEN CAST(dwell_ms AS REAL) END)                             AS avg_dwell_ms
            FROM  events
            WHERE store_id = ? AND is_staff = 0 AND zone_id IS NOT NULL
            GROUP BY zone_id
            ORDER BY visit_count DESC
            """,
            (store_id,),
        ) as cur:
            rows = await cur.fetchall()

    data_confidence = "high" if session_count >= 20 else "low"

    if not rows:
        return {
            "store_id": store_id,
            "zones": [],
            "data_confidence": data_confidence,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    max_visits = max((r[1] for r in rows), default=1) or 1

    zones = [
        {
            "zone_id": r[0],
            "visit_count": r[1],
            "avg_dwell_ms": round(r[2], 2) if r[2] is not None else None,
            "heat_score": round(r[1] / max_visits * 100, 1),
        }
        for r in rows
        if r[1] > 0  # omit zones that were never entered
    ]

    return {
        "store_id": store_id,
        "zones": zones,
        "data_confidence": data_confidence,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_anomalies(db_path: str, store_id: str) -> dict:
    """
    Rule-based anomaly detection.

    Thresholds:
      Queue depth avg > 10  → CRITICAL
      Queue depth avg >  5  → WARN
      Abandonment rate > 50% → WARN
    """
    anomalies: list[dict] = []
    checked_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(db_path) as db:

        # ── Queue depth ────────────────────────────────────────────────────
        async with db.execute(
            """
            SELECT metadata
            FROM   events
            WHERE  store_id   = ? AND is_staff = 0
              AND  event_type = 'BILLING_QUEUE_JOIN'
              AND  metadata   IS NOT NULL
            """,
            (store_id,),
        ) as cur:
            meta_rows = await cur.fetchall()

        depths: list[float] = []
        for (meta_str,) in meta_rows:
            try:
                meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                if meta and meta.get("queue_depth") is not None:
                    depths.append(float(meta["queue_depth"]))
            except (json.JSONDecodeError, TypeError, AttributeError, KeyError):
                pass

        if depths:
            avg_depth = sum(depths) / len(depths)
            if avg_depth > 10:
                anomalies.append({
                    "type": "HIGH_QUEUE_DEPTH",
                    "severity": "CRITICAL",
                    "zone_id": "zone_billing",
                    "value": round(avg_depth, 1),
                    "message": (
                        f"Average billing queue depth {avg_depth:.1f} is critically high — "
                        "risk of customer churn; open additional counters immediately."
                    ),
                    "suggested_action": (
                        "Open all available billing counters immediately. "
                        "Deploy a floor staff member to assist with queue management."
                    ),
                })
            elif avg_depth > 5:
                anomalies.append({
                    "type": "HIGH_QUEUE_DEPTH",
                    "severity": "WARN",
                    "zone_id": "zone_billing",
                    "value": round(avg_depth, 1),
                    "message": (
                        f"Average billing queue depth {avg_depth:.1f} is elevated — "
                        "consider opening an additional counter."
                    ),
                    "suggested_action": (
                        "Open an additional billing counter or redirect "
                        "nearby floor staff to assist."
                    ),
                })

        # ── Billing abandonment ────────────────────────────────────────────
        async with db.execute(
            """
            SELECT
              SUM(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END),
              SUM(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 ELSE 0 END)
            FROM  events
            WHERE store_id = ? AND is_staff = 0
            """,
            (store_id,),
        ) as cur:
            row = await cur.fetchone()
            abandons, joins = (row[0] or 0), (row[1] or 0)

        total = abandons + joins
        if total > 0:
            rate = abandons / total
            if rate > 0.5:
                anomalies.append({
                    "type": "HIGH_ABANDONMENT_RATE",
                    "severity": "WARN",
                    "zone_id": "zone_billing",
                    "value": round(rate * 100, 1),
                    "message": (
                        f"Billing queue abandonment rate is {rate * 100:.1f}% — "
                        "more than half of visitors leave before completing a purchase."
                    ),
                    "suggested_action": (
                        "Investigate wait time at billing counters. "
                        "Consider adding self-checkout or express-lane options "
                        "to reduce drop-off."
                    ),
                })

    return {
        "store_id": store_id,
        "anomalies": anomalies,
        "checked_at": checked_at,
    }
