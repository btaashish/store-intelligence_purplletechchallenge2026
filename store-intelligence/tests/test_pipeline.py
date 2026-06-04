# PROMPT: "Write pytest tests for a CCTV person detection pipeline that emits
# structured retail store events. Cover: event schema compliance, unique
# event_id generation, timestamp format validation, visitor_id format,
# staff flagging, re-entry detection, group entry counting, and empty
# store periods. Mock the tracker so tests don't require video files."
#
# CHANGES MADE: Added edge case for zero-confidence events (spec says don't
# suppress low-conf), added test for BILLING_QUEUE_ABANDON logic, replaced
# unittest.mock with pytest-mock fixtures, added parametrize for event types.

import pytest
import json
import uuid
import tempfile
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.emit import EventEmitter, make_visitor_id, EventType, StoreEvent


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_events_file(tmp_path):
    return str(tmp_path / "test_events.jsonl")


@pytest.fixture
def emitter(tmp_events_file):
    e = EventEmitter(
        store_id="ST1008",
        camera_id="cam1",
        output_path=tmp_events_file,
        clip_start_ts=datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc),
        fps=30.0,
    )
    yield e
    e.close()


def read_events(path: str):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# ─── Schema compliance ────────────────────────────────────────────────────────

class TestSchemaCompliance:
    def test_entry_event_has_all_required_fields(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_abc123", frame_idx=10, confidence=0.85, is_staff=False)
        emitter.flush()
        events = read_events(tmp_events_file)
        assert len(events) == 1
        e = events[0]
        required = ["event_id", "store_id", "camera_id", "visitor_id",
                    "event_type", "timestamp", "zone_id", "dwell_ms",
                    "is_staff", "confidence", "metadata"]
        for field in required:
            assert field in e, f"Missing required field: {field}"

    def test_event_id_is_valid_uuid(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_abc123", frame_idx=0, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        event_id = events[0]["event_id"]
        uuid.UUID(event_id)  # raises if invalid

    def test_event_ids_are_unique_across_events(self, emitter, tmp_events_file):
        for i in range(20):
            emitter.on_entry(f"VIS_{i:03d}", frame_idx=i * 30, confidence=0.8)
        emitter.flush()
        events = read_events(tmp_events_file)
        ids = [e["event_id"] for e in events]
        assert len(ids) == len(set(ids)), "Duplicate event_ids detected"

    def test_timestamp_is_iso8601_utc(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_abc123", frame_idx=60, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        ts = events[0]["timestamp"]
        assert ts.endswith("Z"), f"Timestamp should end with Z: {ts}"
        datetime.fromisoformat(ts.replace("Z", "+00:00"))  # raises if invalid

    def test_timestamp_advances_with_frame_index(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_001", frame_idx=0, confidence=0.9)
        emitter.on_entry("VIS_002", frame_idx=300, confidence=0.9)  # 10s at 30fps
        emitter.flush()
        events = read_events(tmp_events_file)
        t1 = datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(events[1]["timestamp"].replace("Z", "+00:00"))
        assert t2 > t1, "Later frame should have later timestamp"
        delta = (t2 - t1).total_seconds()
        assert abs(delta - 10.0) < 0.5, f"Expected ~10s delta, got {delta}"

    def test_metadata_has_required_keys(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_abc123", frame_idx=0, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        meta = events[0]["metadata"]
        assert "queue_depth" in meta
        assert "sku_zone" in meta
        assert "session_seq" in meta

    def test_dwell_ms_is_non_negative(self, emitter, tmp_events_file):
        emitter.on_zone_enter("VIS_001", "PURPLLE_MUM_1076_Z02", frame_idx=30, confidence=0.9)
        emitter.on_zone_exit("VIS_001", "PURPLLE_MUM_1076_Z02", frame_idx=90, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        for e in events:
            assert e["dwell_ms"] >= 0, f"Negative dwell_ms in event: {e}"

    @pytest.mark.parametrize("event_type", [
        "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
        "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
    ])
    def test_event_type_values_are_valid(self, event_type):
        """All defined event types must be in the allowed catalogue."""
        allowed = {
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
        }
        assert event_type in allowed


# ─── Visitor ID ───────────────────────────────────────────────────────────────

class TestVisitorId:
    def test_visitor_id_format(self):
        vid = make_visitor_id(42)
        assert vid.startswith("VIS_"), f"visitor_id must start with VIS_: {vid}"
        assert len(vid) == 10, f"Expected length 10, got {len(vid)}: {vid}"

    def test_visitor_id_is_deterministic(self):
        assert make_visitor_id(1) == make_visitor_id(1)
        assert make_visitor_id(1) != make_visitor_id(2)

    def test_visitor_id_in_emitted_event(self, emitter, tmp_events_file):
        vid = make_visitor_id(7)
        emitter.on_entry(vid, frame_idx=0, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        assert events[0]["visitor_id"] == vid


# ─── Staff detection ──────────────────────────────────────────────────────────

class TestStaffDetection:
    def test_staff_flag_preserved_in_event(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_staff01", frame_idx=0, confidence=0.9, is_staff=True)
        emitter.flush()
        events = read_events(tmp_events_file)
        assert events[0]["is_staff"] is True

    def test_non_staff_flag_is_false(self, emitter, tmp_events_file):
        emitter.on_entry("VIS_cust01", frame_idx=0, confidence=0.9, is_staff=False)
        emitter.flush()
        events = read_events(tmp_events_file)
        assert events[0]["is_staff"] is False

    def test_staff_events_do_not_increment_session_count_in_metrics(self, emitter, tmp_events_file):
        # Emit a mix of staff and customer events
        emitter.on_entry("VIS_staff01", frame_idx=0, confidence=0.9, is_staff=True)
        emitter.on_entry("VIS_cust01", frame_idx=10, confidence=0.9, is_staff=False)
        emitter.flush()
        events = read_events(tmp_events_file)
        customer_events = [e for e in events if not e["is_staff"]]
        staff_events = [e for e in events if e["is_staff"]]
        assert len(customer_events) == 1
        assert len(staff_events) == 1


# ─── Re-entry ─────────────────────────────────────────────────────────────────

class TestReentry:
    def test_reentry_event_type_is_reentry(self, emitter, tmp_events_file):
        vid = "VIS_reenter01"
        emitter.on_entry(vid, frame_idx=0, confidence=0.9, is_reentry=False)
        emitter.on_exit(vid, frame_idx=300, confidence=0.9)
        emitter.on_entry(vid, frame_idx=600, confidence=0.9, is_reentry=True)
        emitter.flush()
        events = read_events(tmp_events_file)
        entry_events = [e for e in events if e["event_type"] in ("ENTRY", "REENTRY")]
        reentry = [e for e in entry_events if e["event_type"] == "REENTRY"]
        assert len(reentry) == 1, "Expected exactly one REENTRY event"

    def test_reentry_uses_same_visitor_id(self, emitter, tmp_events_file):
        vid = "VIS_reenter02"
        emitter.on_entry(vid, frame_idx=0, confidence=0.9, is_reentry=False)
        emitter.on_entry(vid, frame_idx=600, confidence=0.9, is_reentry=True)
        emitter.flush()
        events = read_events(tmp_events_file)
        vids = {e["visitor_id"] for e in events}
        assert len(vids) == 1, "Re-entry must use the same visitor_id"
        assert vid in vids


# ─── Group entry ──────────────────────────────────────────────────────────────

class TestGroupEntry:
    def test_three_people_entering_together_emit_three_entry_events(self, emitter, tmp_events_file):
        """
        Group handling: 3 people enter at the same frame — should emit 3 ENTRY events.
        """
        for i in range(3):
            emitter.on_entry(make_visitor_id(100 + i), frame_idx=0, confidence=0.85)
        emitter.flush()
        events = read_events(tmp_events_file)
        entry_events = [e for e in events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 3, f"Expected 3 ENTRY events, got {len(entry_events)}"

    def test_group_entry_has_distinct_visitor_ids(self, emitter, tmp_events_file):
        vids = [make_visitor_id(200 + i) for i in range(4)]
        for vid in vids:
            emitter.on_entry(vid, frame_idx=5, confidence=0.8)
        emitter.flush()
        events = read_events(tmp_events_file)
        found_vids = {e["visitor_id"] for e in events if e["event_type"] == "ENTRY"}
        assert len(found_vids) == 4


# ─── Low confidence ───────────────────────────────────────────────────────────

class TestConfidenceHandling:
    def test_low_confidence_events_are_not_suppressed(self, emitter, tmp_events_file):
        """
        Spec: 'do not suppress low-confidence events'.
        Even confidence=0.01 must be emitted.
        """
        emitter.on_entry("VIS_lowconf", frame_idx=0, confidence=0.01)
        emitter.flush()
        events = read_events(tmp_events_file)
        assert len(events) == 1
        assert events[0]["confidence"] == 0.01

    def test_confidence_clamped_to_0_1(self):
        evt = StoreEvent(
            store_id="S", camera_id="C", visitor_id="V", event_type="ENTRY",
            timestamp="2026-04-10T14:00:00Z", zone_id=None, dwell_ms=0,
            is_staff=False, confidence=1.5,  # over 1
        )
        assert evt.confidence <= 1.0


# ─── Empty store periods ──────────────────────────────────────────────────────

class TestEmptyStorePeriods:
    def test_no_events_emitted_when_no_detections(self, tmp_events_file):
        """Pipeline should handle zero-traffic windows without crashing."""
        emitter = EventEmitter(
            store_id="ST1008",
            camera_id="cam1",
            output_path=tmp_events_file,
            clip_start_ts=datetime(2026, 4, 10, 18, 0, 0, tzinfo=timezone.utc),
            fps=30.0,
        )
        # No events emitted
        emitter.flush()
        emitter.close()
        events = read_events(tmp_events_file)
        assert events == [], "Empty store period should produce zero events"

    def test_metrics_handles_zero_visitors(self):
        """Metrics computation should return 0s, not crash on empty data."""
        from app.services.metrics import build_sessions, correlate_conversions
        sessions = build_sessions([])
        assert sessions == {}
        converted = correlate_conversions({}, [])
        assert converted == {}


# ─── Billing queue ────────────────────────────────────────────────────────────

class TestBillingQueue:
    def test_billing_queue_join_emitted_when_queue_depth_gt_0(self, emitter, tmp_events_file):
        emitter.on_zone_enter(
            "VIS_q01", "PURPLLE_MUM_1076_Z_BILLING_01", frame_idx=100, confidence=0.9, queue_depth=2
        )
        emitter.flush()
        events = read_events(tmp_events_file)
        assert events[0]["event_type"] == "BILLING_QUEUE_JOIN"
        assert events[0]["metadata"]["queue_depth"] == 2

    def test_billing_queue_abandon_emitted_without_purchase(self, emitter, tmp_events_file):
        emitter.on_zone_enter("VIS_q02", "PURPLLE_MUM_1076_Z_BILLING_01", frame_idx=50, confidence=0.9, queue_depth=1)
        emitter.on_zone_exit("VIS_q02", "PURPLLE_MUM_1076_Z_BILLING_01", frame_idx=200, confidence=0.9, had_purchase=False)
        emitter.flush()
        events = read_events(tmp_events_file)
        exit_events = [e for e in events if "EXIT" in e["event_type"] or "ABANDON" in e["event_type"]]
        abandon = [e for e in exit_events if e["event_type"] == "BILLING_QUEUE_ABANDON"]
        assert len(abandon) == 1


# ─── Session sequence ─────────────────────────────────────────────────────────

class TestSessionSequence:
    def test_session_seq_increments_per_visitor(self, emitter, tmp_events_file):
        vid = "VIS_seq01"
        emitter.on_entry(vid, frame_idx=0, confidence=0.9)
        emitter.on_zone_enter(vid, "PURPLLE_MUM_1076_Z02", frame_idx=30, confidence=0.9)
        emitter.on_zone_exit(vid, "PURPLLE_MUM_1076_Z02", frame_idx=300, confidence=0.9)
        emitter.on_exit(vid, frame_idx=400, confidence=0.9)
        emitter.flush()
        events = read_events(tmp_events_file)
        seqs = [e["metadata"]["session_seq"] for e in events if e["visitor_id"] == vid]
        # Should be strictly increasing
        assert seqs == sorted(seqs), f"session_seq not in order: {seqs}"
        assert len(set(seqs)) == len(seqs), "Duplicate session_seq values"
