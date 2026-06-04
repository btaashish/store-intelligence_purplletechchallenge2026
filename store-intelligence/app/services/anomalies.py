"""
anomalies.py — Real-time anomaly detection.

Detects:
    BILLING_QUEUE_SPIKE   — queue depth > threshold
    CONVERSION_DROP       — today's rate < 7-day avg * 0.7
    DEAD_ZONE             — no visits in 30 min during store hours
    HIGH_ABANDONMENT      — >50% billing abandonment rate
    TRAFFIC_SPIKE         — unusual visitor surge
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from collections import defaultdict
import uuid

from app.database import get_events, get_transactions
from app.services.metrics import (
    build_sessions, correlate_conversions, today_window, window_n_days_ago
)

logger = logging.getLogger(__name__)

# ─── Thresholds ───────────────────────────────────────────────────────────────

QUEUE_SPIKE_THRESHOLD       = 5     # persons in billing zone simultaneously
CONVERSION_DROP_FACTOR      = 0.70  # today < 7-day_avg * factor → WARN
DEAD_ZONE_MINUTES           = 30    # no visits in X min → INFO
HIGH_ABANDONMENT_THRESHOLD  = 0.50  # >50% abandonment → WARN
STALE_FEED_MINUTES          = 10    # no events in X min → WARN


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_anomaly(
    anomaly_type: str,
    severity: str,
    store_id: str,
    description: str,
    suggested_action: str,
    zone_id: Optional[str] = None,
    metric_value: Optional[float] = None,
    threshold_value: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "anomaly_id": str(uuid.uuid4()),
        "anomaly_type": anomaly_type,
        "severity": severity,
        "store_id": store_id,
        "zone_id": zone_id,
        "detected_at": _now_ts(),
        "description": description,
        "suggested_action": suggested_action,
        "metric_value": metric_value,
        "threshold_value": threshold_value,
    }


async def detect_anomalies(store_id: str) -> List[Dict[str, Any]]:
    """
    Run all anomaly checks for a store. Returns list of active anomalies.
    """
    anomalies = []
    start_ts, end_ts = today_window()

    events = await get_events(store_id, start_ts, end_ts, exclude_staff=True)
    transactions = await get_transactions(store_id, start_ts, end_ts)

    # ── 1. Billing queue spike ─────────────────────────────────────────────

    queue_anomaly = _check_queue_spike(store_id, events)
    if queue_anomaly:
        anomalies.append(queue_anomaly)

    # ── 2. Conversion drop vs 7-day avg ───────────────────────────────────

    conv_anomaly = await _check_conversion_drop(store_id, events, transactions)
    if conv_anomaly:
        anomalies.append(conv_anomaly)

    # ── 3. Dead zones ─────────────────────────────────────────────────────

    dead_zone_anomalies = _check_dead_zones(store_id, events)
    anomalies.extend(dead_zone_anomalies)

    # ── 4. High abandonment ───────────────────────────────────────────────

    abandon_anomaly = _check_high_abandonment(store_id, events)
    if abandon_anomaly:
        anomalies.append(abandon_anomaly)

    return anomalies


def _check_queue_spike(store_id: str, events: List[Dict]) -> Optional[Dict]:
    """Count current billing zone occupancy from recent events."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")

    in_billing = set()
    for evt in events:
        if evt.get("timestamp", "") < window_start:
            continue
        vid = evt["visitor_id"]
        etype = evt["event_type"]
        if etype in ("BILLING_QUEUE_JOIN", "ZONE_ENTER") and evt.get("zone_id") in ("PURPLLE_MUM_1076_Z_BILLING_01", "BILLING"):
            in_billing.add(vid)
        elif etype in ("ZONE_EXIT", "EXIT") and evt.get("zone_id") in ("PURPLLE_MUM_1076_Z_BILLING_01", "BILLING"):
            in_billing.discard(vid)

    depth = len(in_billing)
    if depth >= QUEUE_SPIKE_THRESHOLD:
        severity = "CRITICAL" if depth >= QUEUE_SPIKE_THRESHOLD * 2 else "WARN"
        return _make_anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            store_id=store_id,
            zone_id="PURPLLE_MUM_1076_Z_BILLING_01",
            description=f"Billing queue depth is {depth} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open an additional billing counter or redirect staff to billing area.",
            metric_value=float(depth),
            threshold_value=float(QUEUE_SPIKE_THRESHOLD),
        )
    return None


async def _check_conversion_drop(
    store_id: str,
    today_events: List[Dict],
    today_transactions: List[Dict],
) -> Optional[Dict]:
    """Compare today's conversion rate to 7-day average."""
    # Today
    sessions_today = build_sessions(today_events)
    converted_today = correlate_conversions(sessions_today, today_transactions)
    today_rate = (
        sum(1 for v in converted_today.values() if v) / len(sessions_today)
        if sessions_today else 0.0
    )

    # 7-day historical
    hist_start, hist_end = window_n_days_ago(7)
    hist_events = await get_events(store_id, hist_start, hist_end, exclude_staff=True)
    hist_txns = await get_transactions(store_id, hist_start, hist_end)
    hist_sessions = build_sessions(hist_events)
    hist_converted = correlate_conversions(hist_sessions, hist_txns)
    hist_rate = (
        sum(1 for v in hist_converted.values() if v) / len(hist_sessions)
        if hist_sessions else None
    )

    if hist_rate is None or len(sessions_today) < 5:
        return None  # Not enough data

    threshold = hist_rate * CONVERSION_DROP_FACTOR
    if today_rate < threshold:
        severity = "CRITICAL" if today_rate < hist_rate * 0.50 else "WARN"
        return _make_anomaly(
            anomaly_type="CONVERSION_DROP",
            severity=severity,
            store_id=store_id,
            description=(
                f"Today's conversion rate {today_rate:.1%} is below "
                f"7-day avg {hist_rate:.1%} (threshold: {threshold:.1%})"
            ),
            suggested_action=(
                "Review staff placement on the floor. "
                "Check if any popular zone is understaffed or if promotions need refreshing."
            ),
            metric_value=round(today_rate, 4),
            threshold_value=round(threshold, 4),
        )
    return None


def _check_dead_zones(store_id: str, events: List[Dict]) -> List[Dict]:
    """Detect zones with no visits in the last 30 minutes."""
    # Known customer-facing zones
    MONITORED_ZONES = {
        "PURPLLE_MUM_1076_Z01",
        "PURPLLE_MUM_1076_Z02",
        "PURPLLE_MUM_1076_Z03",
        "PURPLLE_MUM_1076_Z_BILLING_01",
    }

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Find zones with recent activity
    recently_active = set()
    for evt in events:
        if evt.get("timestamp", "") >= cutoff and evt.get("zone_id"):
            recently_active.add(evt["zone_id"])

    # Check which monitored zones have had ANY events today (to avoid false alarms for quiet stores)
    ever_active = set()
    for evt in events:
        if evt.get("zone_id"):
            ever_active.add(evt["zone_id"])

    anomalies = []
    for zone in MONITORED_ZONES:
        if zone in ever_active and zone not in recently_active:
            anomalies.append(_make_anomaly(
                anomaly_type="DEAD_ZONE",
                severity="INFO",
                store_id=store_id,
                zone_id=zone,
                description=f"Zone {zone} has had no visitor activity in the last {DEAD_ZONE_MINUTES} minutes.",
                suggested_action=(
                    f"Consider repositioning a staff member to {zone} "
                    "to re-engage customers or check if display needs refreshing."
                ),
            ))
    return anomalies


def _check_high_abandonment(store_id: str, events: List[Dict]) -> Optional[Dict]:
    """Detect high billing abandonment rate."""
    sessions = build_sessions(events)
    billing_visitors = [s for s in sessions.values() if s["was_in_billing"]]
    if len(billing_visitors) < 3:
        return None

    abandoned = sum(1 for s in billing_visitors if s["abandoned_billing"])
    rate = abandoned / len(billing_visitors)

    if rate >= HIGH_ABANDONMENT_THRESHOLD:
        severity = "CRITICAL" if rate >= 0.70 else "WARN"
        return _make_anomaly(
            anomaly_type="HIGH_ABANDONMENT",
            severity=severity,
            store_id=store_id,
            zone_id="PURPLLE_MUM_1076_Z_BILLING_01",
            description=f"Billing abandonment rate is {rate:.1%} ({abandoned}/{len(billing_visitors)} visitors left queue)",
            suggested_action=(
                "Reduce billing wait time — consider opening a second counter, "
                "or deploying a mobile billing staff member on the floor."
            ),
            metric_value=round(rate, 4),
            threshold_value=HIGH_ABANDONMENT_THRESHOLD,
        )
    return None
