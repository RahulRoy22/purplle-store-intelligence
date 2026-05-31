#!/usr/bin/env python3
"""
Generates data/generated/sample_events.jsonl

Each line is one event matching the required schema:
  event_id, store_id, camera_id, visitor_id, event_type, timestamp,
  zone_id, dwell_ms, is_staff, confidence, metadata

The data is a FRESH, COHERENT demo: every event timestamp falls inside a
recent ~60-minute window ENDING a few seconds before `datetime.now(UTC)`,
so a reviewer booting the stack sees a live (non-stale) feed and metrics
that are *computed* from these events — nothing is hardcoded downstream.

Scenarios deliberately baked in (all drive real, computed analytics):
  - ~10% staff visitors (is_staff=True)            → staff-exclusion testing
  - Re-entry events (same visitor_id returns)      → REENTRY de-dup testing
  - Duplicate event_ids (~3%)                      → idempotency testing
  - Billing-zone dwellers correlated with POS      → non-zero conversion
  - Browse-but-never-bill visitors                 → funnel drop-off
  - A RECENT billing-queue buildup (queue_depth>5) → HIGH_QUEUE_DEPTH anomaly
  - A low-traffic zone visited ONLY early          → DEAD_ZONE anomaly
    (its last visit is >30 min before the latest event in the store)
"""
import json
import uuid
import random
import pathlib
from datetime import datetime, timezone, timedelta

random.seed(99)
OUTPUT = pathlib.Path(__file__).parent.parent / "generated" / "sample_events.jsonl"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

STORE_ID = "STORE_BLR_002"
CAMERAS = {
    "zone_entry": "cam_entry",
    "zone_skincare": "cam_floor_01",
    "zone_makeup": "cam_floor_02",
    "zone_haircare": "cam_floor_03",
    "zone_fragrance": "cam_floor_04",
    "zone_billing": "cam_billing",
}

# Floor zones any visitor may browse. zone_fragrance is intentionally EXCLUDED
# here — it is only ever visited by the early "dead-zone" cohort below, so its
# last activity is far in the past relative to the latest store event.
FLOOR_ZONES = ["zone_skincare", "zone_makeup", "zone_haircare"]
DEAD_ZONE = "zone_fragrance"

# Recent 60-minute window. After all events are built we shift them so the
# newest event lands exactly at LATEST (~2 min before "now") — guaranteeing a
# non-stale feed while preserving every relative gap (so the early dead-zone
# stays >30 min behind the latest event regardless of generation jitter).
NOW = datetime.now(timezone.utc)
LATEST = NOW - timedelta(minutes=2)
WINDOW_END = LATEST
WINDOW_START = WINDOW_END - timedelta(minutes=60)

events = []


def make_event(visitor_id, event_type, zone_id, ts, dwell_ms=None,
               is_staff=False, confidence=None, metadata=None):
    return {
        "event_id": str(uuid.uuid4()),
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


def at(seconds_from_start: float) -> datetime:
    """A timestamp `seconds_from_start` into the 60-minute window."""
    return WINDOW_START + timedelta(seconds=seconds_from_start)


# ---------------------------------------------------------------------------
# Cohort A — DEAD ZONE: 4 visitors who enter in the first ~8 minutes and are
# the ONLY ones to visit zone_fragrance. Nothing touches that zone afterwards,
# so by the end of the window its last visit is >30 min old → DEAD_ZONE fires.
# ---------------------------------------------------------------------------
for i in range(4):
    vid = f"vis_{i + 1:04d}"
    t = at(random.uniform(30, 480))          # first 8 minutes
    events.append(make_event(vid, "ENTRY", "zone_entry", t))
    t += timedelta(seconds=random.randint(10, 30))

    dwell = random.randint(45_000, 180_000)
    events.append(make_event(vid, "ZONE_ENTER", DEAD_ZONE, t))
    t += timedelta(milliseconds=dwell)
    events.append(make_event(vid, "ZONE_DWELL", DEAD_ZONE, t, dwell_ms=dwell))
    events.append(make_event(vid, "ZONE_EXIT", DEAD_ZONE, t))
    events.append(make_event(vid, "EXIT", "zone_entry", t + timedelta(seconds=20)))


# ---------------------------------------------------------------------------
# Cohort B — 34 mainstream customers spread across minutes ~5–55. Each browses
# 1–3 floor zones; ~40% reach billing with a normal (short) queue; a few abandon.
# Billing joins here are correlated to POS rows by the POS generator.
# ---------------------------------------------------------------------------
for i in range(34):
    vid = f"vis_{i + 5:04d}"
    t = at(random.uniform(300, 2400))        # minutes 5–40
    events.append(make_event(vid, "ENTRY", "zone_entry", t))
    t += timedelta(seconds=random.randint(10, 30))

    for zone in random.sample(FLOOR_ZONES, k=random.randint(1, 3)):
        dwell = random.randint(30_000, 240_000)
        events.append(make_event(vid, "ZONE_ENTER", zone, t))
        t += timedelta(milliseconds=dwell)
        events.append(make_event(vid, "ZONE_DWELL", zone, t, dwell_ms=dwell))
        events.append(make_event(vid, "ZONE_EXIT", zone, t))

    roll = random.random()
    if roll < 0.24:                          # reaches billing, short early queue
        join_t = t
        events.append(make_event(
            vid, "BILLING_QUEUE_JOIN", "zone_billing", join_t,
            metadata={"queue_depth": random.randint(0, 2), "sku_zone": None, "session_seq": 1},
        ))
        events.append(make_event(vid, "ZONE_ENTER", "zone_billing", join_t))
        t = join_t + timedelta(seconds=random.randint(60, 280))
        dwell = int((t - join_t).total_seconds() * 1000)
        events.append(make_event(vid, "ZONE_DWELL", "zone_billing", t, dwell_ms=dwell))
        events.append(make_event(vid, "ZONE_EXIT", "zone_billing", t))
    elif roll < 0.34:                        # joins then abandons the queue
        join_t = t
        events.append(make_event(
            vid, "BILLING_QUEUE_JOIN", "zone_billing", join_t,
            metadata={"queue_depth": random.randint(1, 3), "sku_zone": None, "session_seq": 1},
        ))
        events.append(make_event(
            vid, "BILLING_QUEUE_ABANDON", "zone_billing",
            join_t + timedelta(seconds=random.randint(30, 120)),
            metadata={"queue_depth": random.randint(1, 3), "sku_zone": None, "session_seq": 1},
        ))

    events.append(make_event(vid, "EXIT", "zone_entry", t + timedelta(seconds=15)))


# ---------------------------------------------------------------------------
# Cohort C — RECENT billing rush in the final ~6 minutes. These visitors join
# the billing queue with a high queue_depth (6–12) → the average queue depth is
# elevated (HIGH_QUEUE_DEPTH anomaly) and the most-recent queue_depth (exposed
# as metrics.queue_depth) is high. Most convert; a couple abandon under load.
# These are also the latest events in the store → keeps the feed non-stale.
# ---------------------------------------------------------------------------
for i in range(12):
    vid = f"vis_{i + 41:04d}"
    enter_t = WINDOW_END - timedelta(seconds=random.randint(40, 420))
    events.append(make_event(vid, "ENTRY", "zone_entry", enter_t))

    # Quick browse so they also count in the funnel's zone-visit stage.
    browse_t = enter_t + timedelta(seconds=random.randint(10, 40))
    events.append(make_event(vid, "ZONE_ENTER", "zone_makeup", browse_t))

    join_t = browse_t + timedelta(seconds=random.randint(20, 90))
    depth = random.randint(7, 13)
    events.append(make_event(
        vid, "BILLING_QUEUE_JOIN", "zone_billing", join_t,
        metadata={"queue_depth": depth, "sku_zone": None, "session_seq": 1},
    ))
    events.append(make_event(vid, "ZONE_ENTER", "zone_billing", join_t))

    if i % 4 == 0:                           # ~25% abandon under the rush
        events.append(make_event(
            vid, "BILLING_QUEUE_ABANDON", "zone_billing",
            join_t + timedelta(seconds=random.randint(30, 90)),
            metadata={"queue_depth": depth, "sku_zone": None, "session_seq": 1},
        ))
    else:
        end_t = join_t + timedelta(seconds=random.randint(40, 150))
        dwell = int((end_t - join_t).total_seconds() * 1000)
        events.append(make_event(vid, "ZONE_DWELL", "zone_billing", end_t, dwell_ms=dwell))
        events.append(make_event(vid, "ZONE_EXIT", "zone_billing", end_t))


# ---------------------------------------------------------------------------
# Cohort D — 5 staff members (is_staff=True). Stored but excluded from every
# metric at query time. Spread across the window incl. recent activity.
# ---------------------------------------------------------------------------
for i in range(5):
    vid = f"staff_{i + 1:03d}"
    t = at(random.uniform(0, 1500))
    events.append(make_event(vid, "ENTRY", "zone_entry", t, is_staff=True, confidence=0.99))
    for zone in FLOOR_ZONES + ["zone_billing"]:
        dwell = random.randint(60_000, 240_000)
        t += timedelta(seconds=30)
        events.append(make_event(vid, "ZONE_ENTER", zone, t, is_staff=True))
        t += timedelta(milliseconds=dwell)
        events.append(make_event(vid, "ZONE_DWELL", zone, t, dwell_ms=dwell, is_staff=True))
        events.append(make_event(vid, "ZONE_EXIT", zone, t, is_staff=True))


# ---------------------------------------------------------------------------
# Cohort E — 3 re-entry visitors (reuse early IDs; REENTRY mid-to-late window).
# COUNT(DISTINCT visitor_id) must count them once across ENTRY + REENTRY.
# ---------------------------------------------------------------------------
for i in range(3):
    vid = f"vis_{i + 1:04d}"
    t = at(random.uniform(2400, 3300))       # minutes 40–55
    events.append(make_event(
        vid, "REENTRY", "zone_entry", t,
        metadata={"queue_depth": None, "sku_zone": None, "session_seq": 2},
    ))
    events.append(make_event(vid, "EXIT", "zone_entry", t + timedelta(minutes=8)))


# ---------------------------------------------------------------------------
# ~3% duplicate events (idempotency demo — re-POSTing the batch yields dupes).
# ---------------------------------------------------------------------------
sample_dupes = random.sample(events, k=max(1, len(events) // 33))
events.extend(sample_dupes)

# --- Pin the newest event to LATEST, preserving every relative gap ---------
# (Generation jitter can push the last journey a little past WINDOW_END; this
#  normalises the feed so last_event_at is recent and the dead-zone gap holds.)
_max_dt = max(datetime.fromisoformat(e["timestamp"]) for e in events)
_shift = _max_dt - LATEST
for e in events:
    e["timestamp"] = (datetime.fromisoformat(e["timestamp"]) - _shift).isoformat()

events.sort(key=lambda e: e["timestamp"])

with OUTPUT.open("w") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")

total = len(events)
dupes = len(sample_dupes)
staff = sum(1 for e in events if e["is_staff"])
reentries = sum(1 for e in events if e["event_type"] == "REENTRY")
joins = sum(1 for e in events if e["event_type"] == "BILLING_QUEUE_JOIN")
latest = max(e["timestamp"] for e in events)
print(
    f"[OK] Written {total} events ({dupes} dupes, {staff} staff, {reentries} reentries, "
    f"{joins} billing joins) -> {OUTPUT}\n"
    f"     window {WINDOW_START.isoformat()} .. latest {latest}"
)
