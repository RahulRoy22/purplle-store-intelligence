"""
assertions.py -- Contract tests for mock (and real) data files.

Run with: pytest data/mock/assertions.py -v

These tests act as a gate: if real data files from the organizers break
these assertions, the schema has changed and the pipeline must adapt.
"""
import json
import csv
import pathlib
import pytest

GENERATED = pathlib.Path(__file__).parent.parent / "generated"


class TestStoreLayout:
    def setup_method(self):
        path = GENERATED / "store_layout.json"
        assert path.exists(), "Run generate_store_layout.py first"
        self.layout = json.loads(path.read_text())

    def test_has_store_id(self):
        assert isinstance(self.layout["store_id"], str)
        assert len(self.layout["store_id"]) > 0

    def test_has_zones(self):
        assert len(self.layout["zones"]) >= 3

    def test_has_entry_zone(self):
        entry_zones = [z for z in self.layout["zones"] if z["category"] == "ENTRY"]
        assert len(entry_zones) >= 1, "Must have at least one ENTRY zone"

    def test_has_billing_zone(self):
        billing_zones = [z for z in self.layout["zones"] if z["category"] == "BILLING"]
        assert len(billing_zones) >= 1, "Must have at least one BILLING zone"

    def test_all_zones_have_cameras(self):
        for zone in self.layout["zones"]:
            assert len(zone["cameras"]) >= 1, f"Zone {zone['zone_id']} has no cameras"

    def test_zone_ids_unique(self):
        ids = [z["zone_id"] for z in self.layout["zones"]]
        assert len(ids) == len(set(ids)), "zone_id values must be unique"


class TestPosTransactions:
    def setup_method(self):
        path = GENERATED / "pos_transactions.csv"
        assert path.exists(), "Run generate_pos_transactions.py first"
        self.rows = list(csv.DictReader(path.open()))

    def test_has_rows(self):
        assert len(self.rows) > 0

    def test_no_customer_id(self):
        assert "customer_id" not in self.rows[0], (
            "POS data must NOT contain customer_id -- "
            "correlation is done via billing zone window"
        )

    def test_required_columns(self):
        required = {"transaction_id", "store_id", "timestamp", "amount_inr"}
        assert required.issubset(set(self.rows[0].keys()))

    def test_timestamps_are_iso8601(self):
        from datetime import datetime
        for row in self.rows[:5]:
            datetime.fromisoformat(row["timestamp"])

    def test_amounts_positive(self):
        for row in self.rows:
            assert float(row["amount_inr"]) > 0


class TestSampleEvents:
    def setup_method(self):
        path = GENERATED / "sample_events.jsonl"
        assert path.exists(), "Run generate_sample_events.py first"
        self.events = [json.loads(line) for line in path.open()]

    REQUIRED_KEYS = {
        "event_id", "store_id", "camera_id", "visitor_id", "event_type",
        "timestamp", "zone_id", "dwell_ms", "is_staff", "confidence", "metadata",
    }
    VALID_EVENT_TYPES = {
        "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
        "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
    }

    def test_has_events(self):
        assert len(self.events) > 0

    def test_schema_complete(self):
        for ev in self.events:
            missing = self.REQUIRED_KEYS - set(ev.keys())
            assert not missing, f"Event {ev.get('event_id')} missing: {missing}"

    def test_valid_event_types(self):
        for ev in self.events:
            assert ev["event_type"] in self.VALID_EVENT_TYPES, (
                f"Unknown event_type: {ev['event_type']}"
            )

    def test_has_staff_events(self):
        staff = [e for e in self.events if e["is_staff"] is True]
        assert len(staff) > 0, "Must have at least one is_staff=True event for filter testing"

    def test_has_duplicate_event_ids(self):
        ids = [e["event_id"] for e in self.events]
        assert len(ids) > len(set(ids)), "Must have duplicate event_ids for idempotency testing"

    def test_has_reentry_events(self):
        reentries = [e for e in self.events if e["event_type"] == "REENTRY"]
        assert len(reentries) > 0, "Must have REENTRY events"

    def test_confidence_range(self):
        for ev in self.events:
            assert 0.0 <= ev["confidence"] <= 1.0, (
                f"confidence out of range: {ev['confidence']}"
            )

    def test_metadata_has_required_keys(self):
        for ev in self.events:
            meta = ev["metadata"]
            assert isinstance(meta, dict)
            assert "queue_depth" in meta
            assert "sku_zone" in meta
            assert "session_seq" in meta
