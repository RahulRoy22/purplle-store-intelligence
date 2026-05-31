#!/usr/bin/env python3
"""
Generates data/generated/pos_transactions.csv

Schema: transaction_id, store_id, timestamp (ISO-8601 UTC), amount_inr,
        items_count, payment_method, terminal_id

No customer_id — intentional (POS-to-visitor correlation is done via the
billing-zone 5-minute window rule in SQL, not by ID).

Coherent-demo design
--------------------
This generator runs AFTER generate_sample_events.py and READS the generated
events. For a subset of real BILLING_QUEUE_JOIN events it emits a POS row a
short, random delay later (≤ 300 s) — i.e. INSIDE the 5-minute correlation
window — so the API's `conversion_rate` is a genuine computed quantity over
this data, never a hardcoded constant. A little uncorrelated "noise" POS
traffic is added too, exactly as a real till would record walk-up purchases.

If the events file is absent (generator run in isolation) it falls back to a
purely random, time-windowed set so the script still works standalone.
"""
import csv
import json
import random
import pathlib
from datetime import datetime, timezone, timedelta

random.seed(42)

GENERATED = pathlib.Path(__file__).parent.parent / "generated"
GENERATED.mkdir(parents=True, exist_ok=True)
OUTPUT = GENERATED / "pos_transactions.csv"
EVENTS = GENERATED / "sample_events.jsonl"

STORE_ID = "STORE_BLR_002"
TERMINALS = ["terminal_01", "terminal_02"]
PAYMENT_METHODS = ["UPI", "CARD", "CASH", "WALLET"]

# Fraction of real billing joins that result in a correlated (converting) sale.
CONVERT_FRACTION = 0.55
WINDOW_SECONDS = 300  # the API's correlation window; stay strictly inside it


def _txn(idx: int, store_id: str, ts: datetime) -> dict:
    return {
        "transaction_id": f"TXN{idx:05d}",
        "store_id": store_id,
        "timestamp": ts.isoformat(),
        "amount_inr": round(random.uniform(149, 4999), 2),
        "items_count": random.randint(1, 6),
        "payment_method": random.choice(PAYMENT_METHODS),
        "terminal_id": random.choice(TERMINALS),
    }


def _load_billing_joins() -> list[dict]:
    """
    Return non-staff BILLING_QUEUE_JOIN events whose visitor did NOT abandon
    the queue — only those are eligible to become a correlated sale, so the
    funnel shows a realistic purchase < billing drop-off.
    """
    if not EVENTS.exists():
        return []
    joins = []
    abandoned: set[str] = set()
    for line in EVENTS.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("is_staff"):
            continue
        if ev["event_type"] == "BILLING_QUEUE_ABANDON":
            abandoned.add(ev["visitor_id"])
        elif ev["event_type"] == "BILLING_QUEUE_JOIN":
            joins.append(ev)
    return [ev for ev in joins if ev["visitor_id"] not in abandoned]


rows: list[dict] = []
idx = 1
joins = _load_billing_joins()

if joins:
    # De-dupe by visitor (a visitor may appear twice from duplicate events) and
    # correlate a sale to a random subset, placed inside the 5-minute window.
    seen = set()
    unique_joins = []
    for ev in joins:
        key = (ev["visitor_id"], ev["timestamp"])
        if key not in seen:
            seen.add(key)
            unique_joins.append(ev)

    random.shuffle(unique_joins)
    n_convert = max(1, int(len(unique_joins) * CONVERT_FRACTION))
    for ev in unique_joins[:n_convert]:
        join_ts = datetime.fromisoformat(ev["timestamp"])
        delay = random.randint(20, WINDOW_SECONDS - 30)   # 20–270 s, inside window
        rows.append(_txn(idx, ev["store_id"], join_ts + timedelta(seconds=delay)))
        idx += 1

    # A handful of uncorrelated walk-up sales scattered across the same window
    # (these may or may not fall in anyone's billing window — realistic noise).
    span_start = min(datetime.fromisoformat(e["timestamp"]) for e in joins)
    span_end = max(datetime.fromisoformat(e["timestamp"]) for e in joins)
    span = max(1, int((span_end - span_start).total_seconds()))
    for _ in range(4):
        rows.append(_txn(idx, STORE_ID, span_start + timedelta(seconds=random.randint(0, span))))
        idx += 1
else:
    # Standalone fallback: random transactions over a recent 60-minute window.
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=60)
    for _ in range(80):
        rows.append(_txn(idx, STORE_ID, start + timedelta(seconds=random.randint(0, 3600))))
        idx += 1

rows.sort(key=lambda r: r["timestamp"])

with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

print(
    f"[OK] Written {len(rows)} transactions "
    f"({'correlated to billing joins' if joins else 'standalone random'}) -> {OUTPUT}"
)
