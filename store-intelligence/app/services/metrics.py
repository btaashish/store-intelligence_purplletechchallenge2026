"""
metrics.py — Real-time metric computation from event store.

North Star: Offline Conversion Rate = Visitors who purchased ÷ Total unique visitors

All computations work directly from the events table — no pre-aggregated cache.
This means every API call gets fresh data (as required by the spec).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple, Any
from collections import defaultdict

from app.database import get_events, get_transactions

logger = logging.getLogger(__name__)

# ─── Time window helpers ──────────────────────────────────────────────────────

def today_window() -> Tuple[str, str]:
    """Returns (start, end) ISO strings for today's store hours window."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_n_days_ago(n: int) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Session builder ──────────────────────────────────────────────────────────

def build_sessions(events: List[Dict]) -> Dict[str, Dict]:
    """
    Group events by visitor_id into sessions.

    A session = all events for a visitor_id.
    Re-entries share the same visitor_id (handled by tracker Re-ID).

    Returns:
        dict: visitor_id → {
            "events": [...],
            "entry_time": str,
            "exit_time": str,
            "zones_visited": set,
            "was_in_billing": bool,
            "billing_entry_time": str | None,
            "total_dwell_ms": int,
            "abandoned_billing": bool,
        }
    """
    sessions: Dict[str, Dict] = {}

    for evt in events:
        vid = evt["visitor_id"]
        if vid not in sessions:
            sessions[vid] = {
                "visitor_id": vid,
                "events": [],
                "entry_time": None,
                "exit_time": None,
                "zones_visited": set(),
                "was_in_billing": False,
                "billing_entry_time": None,
                "total_dwell_ms": 0,
                "abandoned_billing": False,
            }
        s = sessions[vid]
        s["events"].append(evt)

        etype = evt["event_type"]
        ts = evt["timestamp"]

        if etype in ("ENTRY", "REENTRY"):
            if s["entry_time"] is None:
                s["entry_time"] = ts
        elif etype == "EXIT":
            s["exit_time"] = ts
        elif etype in ("ZONE_ENTER", "ZONE_DWELL", "BILLING_QUEUE_JOIN"):
            zone = evt.get("zone_id")
            if zone:
                s["zones_visited"].add(zone)
            if zone in ("PURPLLE_MUM_1076_Z_BILLING_01", "BILLING") or etype == "BILLING_QUEUE_JOIN":
                s["was_in_billing"] = True
                if s["billing_entry_time"] is None:
                    s["billing_entry_time"] = ts
        elif etype == "BILLING_QUEUE_ABANDON":
            s["abandoned_billing"] = True
        elif etype == "ZONE_EXIT":
            s["total_dwell_ms"] += evt.get("dwell_ms", 0)

    return sessions


# ─── Conversion correlation ────────────────────────────────────────────────────

def correlate_conversions(
    sessions: Dict[str, Dict],
    transactions: List[Dict],
    window_minutes: int = 5,
) -> Dict[str, bool]:
    """
    Match sessions to transactions by time window.

    A visitor is "converted" if they were in the BILLING zone within
    window_minutes before any transaction timestamp.

    Returns:
        dict: visitor_id → True/False (converted)
    """
    converted = {vid: False for vid in sessions}

    # Build billing visitors by time
    billing_intervals: List[Tuple[datetime, datetime, str]] = []
    for vid, s in sessions.items():
        if not s["was_in_billing"] or not s["billing_entry_time"]:
            continue
        entry = datetime.fromisoformat(s["billing_entry_time"].replace("Z", "+00:00"))
        exit_t = datetime.fromisoformat(s["exit_time"].replace("Z", "+00:00")) if s["exit_time"] else entry + timedelta(minutes=10)
        billing_intervals.append((entry, exit_t, vid))

    for txn in transactions:
        txn_ts = datetime.fromisoformat(txn["timestamp"].replace("Z", "+00:00"))
        window_start = txn_ts - timedelta(minutes=window_minutes)

        for (b_entry, b_exit, vid) in billing_intervals:
            # Visitor was at billing in the 5-min window before transaction
            if b_entry <= txn_ts and b_exit >= window_start:
                converted[vid] = True

    return converted


# ─── Core metric computations ─────────────────────────────────────────────────

async def compute_store_metrics(
    store_id: str,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute live store metrics for the given time window."""
    if not start_ts or not end_ts:
        start_ts, end_ts = today_window()

    # Fetch all customer events (staff excluded)
    events = await get_events(
        store_id=store_id,
        start_ts=start_ts,
        end_ts=end_ts,
        exclude_staff=True,
    )

    transactions = await get_transactions(store_id, start_ts, end_ts)

    sessions = build_sessions(events)
    total_sessions = len(sessions)

    if total_sessions == 0:
        return {
            "store_id": store_id,
            "window_start": start_ts,
            "window_end": end_ts,
            "unique_visitors": 0,
            "conversion_rate": 0.0,
            "avg_dwell_sec": 0.0,
            "zone_dwell": [],
            "queue_depth": _estimate_current_queue(events),
            "abandonment_rate": 0.0,
            "total_transactions": len(transactions),
            "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # Conversion rate
    converted = correlate_conversions(sessions, transactions)
    converted_count = sum(1 for v in converted.values() if v)
    conversion_rate = converted_count / total_sessions if total_sessions > 0 else 0.0

    # Average dwell
    total_dwell_ms = sum(s["total_dwell_ms"] for s in sessions.values())
    avg_dwell_sec = (total_dwell_ms / total_sessions / 1000) if total_sessions > 0 else 0.0

    # Zone dwell stats
    zone_dwell_map: Dict[str, List[float]] = defaultdict(list)
    for evt in events:
        if evt["event_type"] in ("ZONE_EXIT", "ZONE_DWELL") and evt.get("zone_id"):
            zone_dwell_map[evt["zone_id"]].append(evt.get("dwell_ms", 0) / 1000)

    zone_dwell = [
        {
            "zone_id": zone,
            "avg_dwell_sec": round(sum(dwells) / len(dwells), 1) if dwells else 0,
            "visit_count": len(dwells),
        }
        for zone, dwells in zone_dwell_map.items()
    ]

    # Abandonment rate
    billing_visitors = sum(1 for s in sessions.values() if s["was_in_billing"])
    abandoned = sum(1 for s in sessions.values() if s["abandoned_billing"])
    abandonment_rate = abandoned / billing_visitors if billing_visitors > 0 else 0.0

    # Current queue depth
    queue_depth = _estimate_current_queue(events)

    return {
        "store_id": store_id,
        "window_start": start_ts,
        "window_end": end_ts,
        "unique_visitors": total_sessions,
        "conversion_rate": round(conversion_rate, 4),
        "avg_dwell_sec": round(avg_dwell_sec, 1),
        "zone_dwell": zone_dwell,
        "queue_depth": queue_depth,
        "abandonment_rate": round(abandonment_rate, 4),
        "total_transactions": len(transactions),
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _estimate_current_queue(events: List[Dict]) -> int:
    """Estimate current queue depth from recent billing zone events."""
    if not events:
        return 0

    # Count visitors who entered billing but haven't exited in recent events
    # Use last 10 minutes of events
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    in_billing = set()
    for evt in events:
        if evt.get("timestamp", "") < recent_ts:
            continue
        etype = evt["event_type"]
        vid = evt["visitor_id"]
        if etype in ("BILLING_QUEUE_JOIN", "ZONE_ENTER") and evt.get("zone_id") in ("PURPLLE_MUM_1076_Z_BILLING_01", "BILLING"):
            in_billing.add(vid)
        elif etype in ("ZONE_EXIT", "EXIT") and evt.get("zone_id") in ("PURPLLE_MUM_1076_Z_BILLING_01", "BILLING"):
            in_billing.discard(vid)

    return len(in_billing)


async def compute_funnel(
    store_id: str,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute conversion funnel: Entry → Zone Visit → Billing → Purchase."""
    if not start_ts or not end_ts:
        start_ts, end_ts = today_window()

    events = await get_events(store_id, start_ts, end_ts, exclude_staff=True)
    transactions = await get_transactions(store_id, start_ts, end_ts)
    sessions = build_sessions(events)
    converted = correlate_conversions(sessions, transactions)

    total = len(sessions)
    visited_zone = sum(1 for s in sessions.values() if len(s["zones_visited"]) > 0)
    reached_billing = sum(1 for s in sessions.values() if s["was_in_billing"])
    purchased = sum(1 for v, conv in converted.items() if conv)

    def dropoff(current, previous):
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 1)

    stages = [
        {"stage": "Entry", "count": total, "dropoff_pct": 0.0},
        {"stage": "Zone Visit", "count": visited_zone, "dropoff_pct": dropoff(visited_zone, total)},
        {"stage": "Billing Queue", "count": reached_billing, "dropoff_pct": dropoff(reached_billing, visited_zone)},
        {"stage": "Purchase", "count": purchased, "dropoff_pct": dropoff(purchased, reached_billing)},
    ]

    data_confidence = "LOW" if total < 20 else "HIGH"

    return {
        "store_id": store_id,
        "window_start": start_ts,
        "window_end": end_ts,
        "stages": stages,
        "data_confidence": data_confidence,
    }


async def compute_heatmap(
    store_id: str,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Zone visit frequency + avg dwell normalised 0–100."""
    if not start_ts or not end_ts:
        start_ts, end_ts = today_window()

    events = await get_events(store_id, start_ts, end_ts, exclude_staff=True)

    zone_visits: Dict[str, int] = defaultdict(int)
    zone_dwell_total: Dict[str, float] = defaultdict(float)

    for evt in events:
        if evt["event_type"] in ("ZONE_ENTER", "BILLING_QUEUE_JOIN") and evt.get("zone_id"):
            zone_visits[evt["zone_id"]] += 1
        if evt["event_type"] in ("ZONE_EXIT", "ZONE_DWELL") and evt.get("zone_id"):
            zone_dwell_total[evt["zone_id"]] += evt.get("dwell_ms", 0) / 1000

    if not zone_visits:
        return {
            "store_id": store_id,
            "window_start": start_ts,
            "window_end": end_ts,
            "cells": [],
            "data_confidence": "LOW",
        }

    max_visits = max(zone_visits.values())
    sessions = build_sessions(events)
    total_sessions = len(sessions)

    cells = []
    for zone, count in zone_visits.items():
        avg_dwell = zone_dwell_total[zone] / count if count > 0 else 0
        normalised = round((count / max_visits) * 100, 1) if max_visits > 0 else 0
        cells.append({
            "zone_id": zone,
            "visit_count": count,
            "avg_dwell_sec": round(avg_dwell, 1),
            "normalised_score": normalised,
        })

    cells.sort(key=lambda c: c["normalised_score"], reverse=True)

    return {
        "store_id": store_id,
        "window_start": start_ts,
        "window_end": end_ts,
        "cells": cells,
        "data_confidence": "LOW" if total_sessions < 20 else "HIGH",
    }
