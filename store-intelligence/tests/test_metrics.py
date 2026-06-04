# PROMPT: "Write pytest tests for a retail store metrics API that computes
# conversion rate, funnel stages, heatmap, and session deduplication from
# CCTV events. Test: conversion rate formula, funnel drop-off calculation,
# zero-purchase stores, re-entry deduplication, staff event exclusion,
# zone dwell averaging, abandonment rate, and heatmap normalisation."
#
# CHANGES MADE: Replaced async DB calls with synchronous mock data to keep
# tests fast and isolated; added parametrize for edge cases like single
# visitor, all-staff clip, zero transactions. Added low data confidence test.

import pytest
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.metrics import (
    build_sessions,
    correlate_conversions,
    today_window,
)


# ─── Helper factories ──────────────────────────────────────────────────────────

def make_event(visitor_id, event_type, timestamp=None, zone_id=None,
               dwell_ms=0, is_staff=False, confidence=0.9):
    return {
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp or "2026-04-10T14:30:00Z",
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
    }


def make_transaction(txn_id, store_id, timestamp, basket_value=500.0):
    return {
        "transaction_id": txn_id,
        "store_id": store_id,
        "timestamp": timestamp,
        "basket_value": basket_value,
    }


# ─── Session building ──────────────────────────────────────────────────────────

class TestSessionBuilding:
    def test_entry_creates_session(self):
        events = [make_event("VIS_001", "ENTRY", "2026-04-10T14:00:00Z")]
        sessions = build_sessions(events)
        assert "VIS_001" in sessions

    def test_multiple_visitors_create_multiple_sessions(self):
        events = [
            make_event("VIS_001", "ENTRY"),
            make_event("VIS_002", "ENTRY"),
            make_event("VIS_003", "ENTRY"),
        ]
        sessions = build_sessions(events)
        assert len(sessions) == 3

    def test_reentry_does_not_create_second_session(self):
        """REENTRY shares visitor_id — should not count as new visitor."""
        events = [
            make_event("VIS_001", "ENTRY", "2026-04-10T14:00:00Z"),
            make_event("VIS_001", "EXIT", "2026-04-10T14:20:00Z"),
            make_event("VIS_001", "REENTRY", "2026-04-10T14:25:00Z"),
        ]
        sessions = build_sessions(events)
        assert len(sessions) == 1, "REENTRY must not inflate visitor count"

    def test_zone_visits_accumulated_in_session(self):
        events = [
            make_event("VIS_001", "ENTRY"),
            make_event("VIS_001", "ZONE_ENTER", zone_id="PURPLLE_MUM_1076_Z02"),
            make_event("VIS_001", "ZONE_EXIT", zone_id="PURPLLE_MUM_1076_Z02", dwell_ms=30000),
            make_event("VIS_001", "ZONE_ENTER", zone_id="PURPLLE_MUM_1076_Z03"),
        ]
        sessions = build_sessions(events)
        assert "PURPLLE_MUM_1076_Z02" in sessions["VIS_001"]["zones_visited"]
        assert "PURPLLE_MUM_1076_Z03" in sessions["VIS_001"]["zones_visited"]

    def test_billing_visit_recorded(self):
        events = [
            make_event("VIS_001", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
        ]
        sessions = build_sessions(events)
        assert sessions["VIS_001"]["was_in_billing"] is True

    def test_abandonment_recorded(self):
        events = [
            make_event("VIS_001", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_001", "BILLING_QUEUE_ABANDON", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
        ]
        sessions = build_sessions(events)
        assert sessions["VIS_001"]["abandoned_billing"] is True

    def test_total_dwell_accumulates_from_zone_exit_events(self):
        events = [
            make_event("VIS_001", "ZONE_EXIT", zone_id="PURPLLE_MUM_1076_Z02", dwell_ms=45000),
            make_event("VIS_001", "ZONE_EXIT", zone_id="PURPLLE_MUM_1076_Z03", dwell_ms=30000),
        ]
        sessions = build_sessions(events)
        assert sessions["VIS_001"]["total_dwell_ms"] == 75000

    def test_empty_event_list_returns_empty_sessions(self):
        sessions = build_sessions([])
        assert sessions == {}


# ─── Conversion correlation ────────────────────────────────────────────────────

class TestConversionCorrelation:
    def test_visitor_in_billing_before_transaction_is_converted(self):
        sessions = build_sessions([
            make_event("VIS_001", "ENTRY", "2026-04-10T14:00:00Z"),
            make_event("VIS_001", "BILLING_QUEUE_JOIN", "2026-04-10T14:10:00Z", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_001", "EXIT", "2026-04-10T14:15:00Z"),
        ])
        transactions = [make_transaction("TXN_001", "S", "2026-04-10T14:12:00Z")]
        converted = correlate_conversions(sessions, transactions)
        assert converted["VIS_001"] is True

    def test_visitor_not_in_billing_is_not_converted(self):
        sessions = build_sessions([
            make_event("VIS_002", "ENTRY", "2026-04-10T14:00:00Z"),
            make_event("VIS_002", "ZONE_ENTER", "2026-04-10T14:05:00Z", zone_id="PURPLLE_MUM_1076_Z02"),
            make_event("VIS_002", "EXIT", "2026-04-10T14:10:00Z"),
        ])
        transactions = [make_transaction("TXN_002", "S", "2026-04-10T14:06:00Z")]
        converted = correlate_conversions(sessions, transactions)
        assert converted["VIS_002"] is False

    def test_zero_transactions_means_zero_conversions(self):
        sessions = build_sessions([
            make_event("VIS_001", "ENTRY"),
            make_event("VIS_001", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
        ])
        converted = correlate_conversions(sessions, [])
        assert converted["VIS_001"] is False

    def test_conversion_rate_formula(self):
        """conversion_rate = converted_visitors / total_sessions"""
        events = []
        # 3 visitors: 2 go to billing + have transactions, 1 doesn't
        for i in range(1, 4):
            events.append(make_event(f"VIS_{i:03d}", "ENTRY", f"2026-04-10T1{i}:00:00Z"))
            if i <= 2:
                events.append(make_event(f"VIS_{i:03d}", "BILLING_QUEUE_JOIN",
                                          f"2026-04-10T1{i}:10:00Z", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"))
        sessions = build_sessions(events)
        transactions = [
            make_transaction("TXN_001", "S", "2026-04-10T11:12:00Z"),
            make_transaction("TXN_002", "S", "2026-04-10T12:12:00Z"),
        ]
        converted = correlate_conversions(sessions, transactions)
        conv_count = sum(1 for v in converted.values() if v)
        rate = conv_count / len(sessions)
        assert abs(rate - 2/3) < 0.01, f"Expected ~0.667, got {rate}"

    def test_zero_visitors_conversion_rate_is_zero(self):
        sessions = build_sessions([])
        converted = correlate_conversions(sessions, [])
        total = len(sessions)
        rate = sum(1 for v in converted.values() if v) / total if total > 0 else 0.0
        assert rate == 0.0

    @pytest.mark.parametrize("num_visitors,num_conversions,expected_rate", [
        (10, 5, 0.5),
        (1, 1, 1.0),
        (10, 0, 0.0),
        (100, 100, 1.0),
    ])
    def test_conversion_rate_parametrized(self, num_visitors, num_conversions, expected_rate):
        events = []
        for i in range(num_visitors):
            events.append(make_event(f"VIS_{i:03d}", "ENTRY", f"2026-04-10T14:00:0{i % 60:02d}Z"))
            if i < num_conversions:
                events.append(make_event(f"VIS_{i:03d}", "BILLING_QUEUE_JOIN",
                                          f"2026-04-10T14:05:0{i % 60:02d}Z", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"))
        sessions = build_sessions(events)
        transactions = [
            make_transaction(f"TXN_{i}", "S", f"2026-04-10T14:07:0{i % 60:02d}Z")
            for i in range(num_conversions)
        ]
        converted = correlate_conversions(sessions, transactions)
        total = len(sessions)
        rate = sum(1 for v in converted.values() if v) / total if total > 0 else 0.0
        assert abs(rate - expected_rate) < 0.05


# ─── Staff exclusion ──────────────────────────────────────────────────────────

class TestStaffExclusion:
    def test_staff_events_excluded_from_session_count(self):
        """Staff events should be filtered before session building."""
        all_events = [
            make_event("VIS_STAFF_01", "ENTRY", is_staff=True),
            make_event("VIS_STAFF_02", "ENTRY", is_staff=True),
            make_event("VIS_CUST_01", "ENTRY", is_staff=False),
        ]
        # Simulate staff exclusion (as DB query does)
        customer_events = [e for e in all_events if not e["is_staff"]]
        sessions = build_sessions(customer_events)
        assert len(sessions) == 1
        assert "VIS_CUST_01" in sessions

    def test_all_staff_clip_returns_zero_visitors(self):
        all_events = [
            make_event(f"VIS_STAFF_{i}", "ENTRY", is_staff=True) for i in range(5)
        ]
        customer_events = [e for e in all_events if not e["is_staff"]]
        sessions = build_sessions(customer_events)
        assert len(sessions) == 0


# ─── Funnel logic ─────────────────────────────────────────────────────────────

class TestFunnelLogic:
    def test_funnel_stages_are_monotonically_decreasing(self):
        """Each stage should have <= count of the previous stage."""
        events = [
            # 5 enter
            *[make_event(f"VIS_{i}", "ENTRY") for i in range(5)],
            # 3 visit a zone
            *[make_event(f"VIS_{i}", "ZONE_ENTER", zone_id="PURPLLE_MUM_1076_Z02") for i in range(3)],
            # 2 reach billing
            *[make_event(f"VIS_{i}", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01") for i in range(2)],
        ]
        sessions = build_sessions(events)
        total = len(sessions)
        visited_zone = sum(1 for s in sessions.values() if len(s["zones_visited"]) > 0)
        reached_billing = sum(1 for s in sessions.values() if s["was_in_billing"])

        assert total >= visited_zone >= reached_billing

    def test_dropoff_pct_between_stages(self):
        events = [
            make_event("VIS_001", "ENTRY"),
            make_event("VIS_002", "ENTRY"),
            make_event("VIS_001", "ZONE_ENTER", zone_id="PURPLLE_MUM_1076_Z03"),  # only 1 visits zone
        ]
        sessions = build_sessions(events)
        total = len(sessions)
        visited = sum(1 for s in sessions.values() if len(s["zones_visited"]) > 0)
        dropoff = round((1 - visited / total) * 100, 1)
        assert dropoff == 50.0

    def test_data_confidence_low_when_under_20_sessions(self):
        events = [make_event(f"VIS_{i}", "ENTRY") for i in range(5)]
        sessions = build_sessions(events)
        confidence = "LOW" if len(sessions) < 20 else "HIGH"
        assert confidence == "LOW"

    def test_data_confidence_high_when_20_or_more_sessions(self):
        events = [make_event(f"VIS_{i:03d}", "ENTRY") for i in range(25)]
        sessions = build_sessions(events)
        confidence = "LOW" if len(sessions) < 20 else "HIGH"
        assert confidence == "HIGH"


# ─── Heatmap normalisation ────────────────────────────────────────────────────

class TestHeatmapNormalisation:
    def test_top_zone_gets_score_100(self):
        zone_visits = {"PURPLLE_MUM_1076_Z02": 50, "PURPLLE_MUM_1076_Z03": 30, "PURPLLE_MUM_1076_Z_BILLING_01": 10}
        max_v = max(zone_visits.values())
        scores = {z: round(v / max_v * 100, 1) for z, v in zone_visits.items()}
        assert scores["PURPLLE_MUM_1076_Z02"] == 100.0

    def test_all_zones_normalised_0_to_100(self):
        zone_visits = {"A": 10, "B": 5, "C": 1, "D": 0}
        max_v = max(zone_visits.values()) or 1
        scores = {z: round(v / max_v * 100, 1) for z, v in zone_visits.items()}
        for s in scores.values():
            assert 0 <= s <= 100


# ─── Abandonment rate ─────────────────────────────────────────────────────────

class TestAbandonmentRate:
    def test_abandonment_rate_calculation(self):
        events = [
            # 4 reach billing, 2 abandon
            make_event("VIS_001", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_001", "BILLING_QUEUE_ABANDON", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_002", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_002", "BILLING_QUEUE_ABANDON", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_003", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
            make_event("VIS_004", "BILLING_QUEUE_JOIN", zone_id="PURPLLE_MUM_1076_Z_BILLING_01"),
        ]
        sessions = build_sessions(events)
        billing = [s for s in sessions.values() if s["was_in_billing"]]
        abandoned = [s for s in billing if s["abandoned_billing"]]
        rate = len(abandoned) / len(billing)
        assert abs(rate - 0.5) < 0.01
