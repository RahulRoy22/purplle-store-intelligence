#!/usr/bin/env python3
"""
Generates ADDITIONAL demo stores and APPENDS them to
data/generated/sample_events.jsonl (written first by generate_sample_events.py).

Why this exists
---------------
The primary seed (generate_sample_events.py) produces one rich store,
STORE_BLR_002 — the store the acceptance gate queries. To demonstrate the
dashboard's multi-store selector with a realistic chain, this script appends a
couple more synthetic stores (different cities, different scale) in the SAME
recent ~60-minute window so the live feed stays non-stale.

This is clearly *generated demo data*, identical in nature to the existing
single-store seed — not real footage detections. The challenge explicitly
permits replaying events into the API. The downstream POS generator
(generate_pos_transactions.py) already keys correlated transactions off each
event's own store_id, so running this BEFORE it yields a computed, non-zero
conversion_rate for every store with no extra wiring.

Run order (see docker-compose seed service):
    generate_store_layout.py
    generate_sample_events.py        # writes STORE_BLR_002 (overwrites file)
    generate_extra_stores.py         # APPENDS extra stores  <-- this file
    generate_pos_transactions.py     # reads all stores, writes correlated POS

stdlib only (random/uuid/json) — no third-party deps, matching the repo rule.
"""
import json
import uuid
import random
import pathlib
from datetime import datetime, timezone, timedelta

random.seed(123)
OUTPUT = pathlib.Path(__file__).parent.parent / "generated" / "sample_events.jsonl"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# Extra demo stores: (store_id, customer_count, staff_count). Scales differ so
# the stores look distinct in the dashboard. STORE_BLR_002 is NOT here — it is
# the primary seed and the gate's canonical store.
EXTRA_STORES = [
    ("STORE_DEL_001", 38, 4),
    ("STORE_MUM_003", 22, 3),
]

FLOOR_ZONES = ["zone_skincare", "zone_makeup", "zone_haircare", "zone_fragrance"]

NOW = datetime.now(timezone.utc)
LATEST = NOW - timedelta(minutes=2)          # newest event ~2 min ago → non-stale
WINDOW_MIN = 58


def _meta(queue_depth=None, seq=1):
    return {"queue_depth": queue_depth, "sku_zone": None, "session_seq": seq}


def make_event(store_id, vid, etype, zone, ts, dwell_ms=None, is_staff=False,
               queue_depth=None, seq=1):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "cam_entry" if zone in (None, "zone_entry") else "cam_floor",
        "visitor_id": vid,
        "event_type": etype,
        "timestamp": ts.isoformat(),
        "zone_id": zone,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(random.uniform(0.80, 0.99), 3),
        "metadata": _meta(queue_depth, seq),
    }


def at(minutes_ago: float) -> datetime:
    return LATEST - timedelta(minutes=minutes_ago) + timedelta(seconds=random.randint(0, 40))


def build_store(store_id: str, n_cust: int, n_staff: int) -> list[dict]:
    events: list[dict] = []

    # Mainstream customers
    for i in range(n_cust):
        vid = f"vis_{store_id[-3:]}_{i:03d}"
        t = at(random.uniform(3, WINDOW_MIN))
        events.append(make_event(store_id, vid, "ENTRY", None, t))

        for z in random.sample(FLOOR_ZONES, k=random.randint(1, 3)):
            t += timedelta(seconds=random.randint(20, 60))
            d = random.randint(30_000, 180_000)
            events.append(make_event(store_id, vid, "ZONE_ENTER", z, t))
            t += timedelta(milliseconds=d)
            events.append(make_event(store_id, vid, "ZONE_DWELL", z, t, dwell_ms=d))
            events.append(make_event(store_id, vid, "ZONE_EXIT", z, t))

        roll = random.random()
        if roll < 0.45:                       # reaches billing
            qd = random.randint(0, 6)
            events.append(make_event(store_id, vid, "BILLING_QUEUE_JOIN", "zone_billing", t, queue_depth=qd))
            events.append(make_event(store_id, vid, "ZONE_ENTER", "zone_billing", t))
            t2 = t + timedelta(seconds=random.randint(40, 200))
            if roll < 0.10:                   # ~10% abandon
                events.append(make_event(store_id, vid, "BILLING_QUEUE_ABANDON", "zone_billing", t2, queue_depth=qd))
            else:
                d = int((t2 - t).total_seconds() * 1000)
                events.append(make_event(store_id, vid, "ZONE_DWELL", "zone_billing", t2, dwell_ms=d))
                events.append(make_event(store_id, vid, "ZONE_EXIT", "zone_billing", t2))
            t = t2
        events.append(make_event(store_id, vid, "EXIT", None, t + timedelta(seconds=15)))

    # A few re-entries (counted once by COUNT(DISTINCT visitor_id))
    for i in range(2):
        vid = f"vis_{store_id[-3:]}_{i:03d}"
        t = at(random.uniform(3, 20))
        events.append(make_event(store_id, vid, "REENTRY", None, t, seq=2))
        events.append(make_event(store_id, vid, "EXIT", None, t + timedelta(minutes=5)))

    # Staff (stored, excluded from customer metrics at query time)
    for s in range(n_staff):
        vid = f"staff_{store_id[-3:]}_{s}"
        t = at(random.uniform(5, WINDOW_MIN))
        events.append(make_event(store_id, vid, "ENTRY", None, t, is_staff=True))
        for z in FLOOR_ZONES + ["zone_billing"]:
            t += timedelta(seconds=30)
            d = random.randint(60_000, 180_000)
            events.append(make_event(store_id, vid, "ZONE_DWELL", z, t, dwell_ms=d, is_staff=True))

    return events


def main() -> None:
    if not OUTPUT.exists():
        raise SystemExit(
            f"{OUTPUT} not found — run generate_sample_events.py first "
            "(this script appends to it)."
        )
    all_events: list[dict] = []
    for store_id, n_cust, n_staff in EXTRA_STORES:
        evs = build_store(store_id, n_cust, n_staff)
        all_events.extend(evs)
        print(f"[OK] {store_id}: {len(evs)} events "
              f"({n_cust} customers, {n_staff} staff)")

    with OUTPUT.open("a", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev) + "\n")

    print(f"[OK] Appended {len(all_events)} events for "
          f"{len(EXTRA_STORES)} extra stores -> {OUTPUT}")


if __name__ == "__main__":
    main()
