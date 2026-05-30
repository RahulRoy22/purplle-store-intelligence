#!/usr/bin/env python3
"""
Generates data/generated/sample_events.jsonl

Each line is one event matching the required schema:
  event_id, store_id, camera_id, visitor_id, event_type, timestamp,
  zone_id, dwell_ms, is_staff, confidence, metadata

Scenarios deliberately baked in:
  - ~5% staff visitors (is_staff=True) for filter testing
  - Re-entry events (same visitor_id leaves and comes back)
  - Duplicate event_ids (tests idempotency -- 3% duplication rate)
  - Billing zone dwellers who correlate with POS transactions
  - Visitors who browse but never reach billing (funnel drop-off)
"""
import json
import uuid
import random
import pathlib
from datetime import datetime, timezone, timedelta

random.seed(99)
OUTPUT = pathlib.Path(__file__).parent.parent / "generated" / "sample_events.jsonl"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

STORE_ID = "store_001"
CAMERAS = {
    "zone_entry": "cam_entry",
    "zone_skincare": "cam_floor_01",
    "zone_makeup": "cam_floor_02",
    "zone_haircare": "cam_floor_03",
    "zone_fragrance": "cam_floor_04",
    "zone_billing": "cam_billing",
}
SESSION_START = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)

events = []


def make_event(visitor_id, event_type, zone_id, ts, dwell_ms=None,
               is_staff=False, confidence=None, metadata=None):
    eid = str(uuid.uuid4())
    return {
        "event_id": eid,
        "store_id": STORE_ID,
        "camera_id": CAMERAS.get(zone_id, "cam_entry"),
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence if confidence is not None else round(random.uniform(0.75, 0.99), 3),
        "metadata": metadata or {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# --- 60 customer visitors ---
for i in range(60):
    vid = f"vis_{i + 1:04d}"
    t = SESSION_START + timedelta(minutes=random.randint(0, 420))

    events.append(make_event(vid, "ENTRY", "zone_entry", t))
    t += timedelta(seconds=random.randint(10, 30))

    zones_visited = random.sample(
        ["zone_skincare", "zone_makeup", "zone_haircare", "zone_fragrance"],
        k=random.randint(1, 3)
    )
    for zone in zones_visited:
        dwell = random.randint(30_000, 600_000)
        events.append(make_event(vid, "ZONE_ENTER", zone, t))
        t += timedelta(milliseconds=dwell)
        events.append(make_event(vid, "ZONE_DWELL", zone, t, dwell_ms=dwell))
        events.append(make_event(vid, "ZONE_EXIT", zone, t))

    # 40% reach billing
    if random.random() < 0.40:
        billing_entry_t = t
        events.append(make_event(
            vid, "BILLING_QUEUE_JOIN", "zone_billing", billing_entry_t,
            metadata={"queue_depth": random.randint(0, 4), "sku_zone": None, "session_seq": 1}
        ))
        t += timedelta(seconds=random.randint(60, 300))
        events.append(make_event(vid, "ZONE_ENTER", "zone_billing", billing_entry_t))
        dwell = int((t - billing_entry_t).total_seconds() * 1000)
        events.append(make_event(vid, "ZONE_DWELL", "zone_billing", t, dwell_ms=dwell))
        events.append(make_event(vid, "ZONE_EXIT", "zone_billing", t))

    events.append(make_event(vid, "EXIT", "zone_entry", t + timedelta(seconds=15)))

# --- 5 staff members ---
for i in range(5):
    vid = f"staff_{i + 1:03d}"
    t = SESSION_START + timedelta(minutes=random.randint(0, 30))
    events.append(make_event(vid, "ENTRY", "zone_entry", t, is_staff=True, confidence=0.99))
    for zone in ["zone_skincare", "zone_makeup", "zone_haircare", "zone_fragrance", "zone_billing"]:
        dwell = random.randint(60_000, 3_600_000)
        t += timedelta(seconds=30)
        events.append(make_event(vid, "ZONE_ENTER", zone, t, is_staff=True))
        t += timedelta(milliseconds=dwell)
        events.append(make_event(vid, "ZONE_DWELL", zone, t, dwell_ms=dwell, is_staff=True))
        events.append(make_event(vid, "ZONE_EXIT", zone, t, is_staff=True))

# --- 3 re-entry visitors (reuse early visitor IDs so same session merges) ---
for i in range(3):
    vid = f"vis_{i + 1:04d}"
    t = SESSION_START + timedelta(hours=2, minutes=random.randint(0, 60))
    events.append(make_event(
        vid, "REENTRY", "zone_entry", t,
        metadata={"queue_depth": None, "sku_zone": None, "session_seq": 2}
    ))
    events.append(make_event(vid, "EXIT", "zone_entry", t + timedelta(minutes=15)))

# --- 3% duplicate events (idempotency test) ---
sample_dupes = random.sample(events, k=max(1, len(events) // 33))
events.extend(sample_dupes)

events.sort(key=lambda e: e["timestamp"])

with OUTPUT.open("w") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")

total = len(events)
dupes = len(sample_dupes)
staff = sum(1 for e in events if e["is_staff"])
reentries = sum(1 for e in events if e["event_type"] == "REENTRY")
print(f"[OK] Written {total} events ({dupes} intentional dupes, {staff} staff, {reentries} reentries) -> {OUTPUT}")
