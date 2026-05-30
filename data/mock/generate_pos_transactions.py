#!/usr/bin/env python3
"""
Generates data/generated/pos_transactions.csv

Schema: transaction_id, store_id, timestamp (ISO-8601 UTC), amount_inr,
        items_count, payment_method, terminal_id

No customer_id — intentional (POS-to-visitor correlation is done via
the billing zone 5-minute window rule, not by ID).
"""
import csv
import random
import pathlib
from datetime import datetime, timezone, timedelta

random.seed(42)

OUTPUT = pathlib.Path(__file__).parent.parent / "generated" / "pos_transactions.csv"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

STORE_ID = "store_001"
TERMINALS = ["terminal_01", "terminal_02"]
PAYMENT_METHODS = ["UPI", "CARD", "CASH", "WALLET"]
SESSION_START = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)
SESSION_HOURS = 8
NUM_TRANSACTIONS = 80

rows = []
for i in range(NUM_TRANSACTIONS):
    offset_secs = random.randint(0, SESSION_HOURS * 3600)
    ts = SESSION_START + timedelta(seconds=offset_secs)
    amount = round(random.uniform(149, 4999), 2)
    items = random.randint(1, 6)
    rows.append({
        "transaction_id": f"TXN{i + 1:05d}",
        "store_id": STORE_ID,
        "timestamp": ts.isoformat(),
        "amount_inr": amount,
        "items_count": items,
        "payment_method": random.choice(PAYMENT_METHODS),
        "terminal_id": random.choice(TERMINALS),
    })

rows.sort(key=lambda r: r["timestamp"])

with OUTPUT.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"[OK] Written {len(rows)} transactions -> {OUTPUT}")
