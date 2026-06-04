"""
Anomaly detection engine.
Detects: queue spikes, conversion drops, dead zones, stale feeds, abandonment spikes.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

from .models import AnomaliesResponse, Anomaly, AnomalySeverity, AnomalyType
from .core.database import EventRecord

logger = logging.getLogger(__name__)

# Thresholds
QUEUE_SPIKE_THRESHOLD = 4          # queue depth >= 4 = spike
DEAD_ZONE_MINUTES = 30             # no zone visits in 30 min
STALE_FEED_MINUTES = 10            # no events in 10 min = stale
CONVERSION_DROP_THRESHOLD = 0.20   # 20% relative drop vs baseline
ABANDONMENT_SPIKE_THRESHOLD = 0.60 # >60% abandonment rate


def get_anomalies(store_id: str, db: Session) -> AnomaliesResponse:
    """Detect active anomalies for a store."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    anomalies: List[Anomaly] = []

    # Use latest event timestamp as "now" for video data
    latest_ts_row = (
        db.query(func.max(EventRecord.timestamp))
        .filter(EventRecord.store_id == store_id)
        .scalar()
    )
    if not latest_ts_row:
        return AnomaliesResponse(store_id=store_id, anomalies=[], checked_at=now_str)

    try:
        latest_ts = datetime.strptime(latest_ts_row, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        latest_ts = datetime.now(timezone.utc)

    ref_now = latest_ts

    # ---- 1. Billing Queue Spike ----
    latest_queue_event = (
        db.query(EventRecord)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.queue_depth.isnot(None),
        )
        .order_by(EventRecord.timestamp.desc())
        .first()
    )
    if latest_queue_event and (latest_queue_event.queue_depth or 0) >= QUEUE_SPIKE_THRESHOLD:
        severity = (
            AnomalySeverity.CRITICAL
            if latest_queue_event.queue_depth >= 7
            else AnomalySeverity.WARN
        )
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=severity,
            detected_at=latest_queue_event.timestamp,
            description=f"Billing queue depth is {latest_queue_event.queue_depth} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open additional billing counter or redirect customers. Consider calling a supervisor.",
            zone_id="BILLING",
            metric_value=float(latest_queue_event.queue_depth),
            threshold_value=float(QUEUE_SPIKE_THRESHOLD),
        ))

    # ---- 2. Dead Zone Detection ----
    cutoff_dead = (ref_now - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_zones = (
        db.query(distinct(EventRecord.zone_id))
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.zone_id.isnot(None),
            EventRecord.event_type == "ZONE_ENTER",
        )
        .all()
    )
    all_zone_ids = [r[0] for r in all_zones if r[0] and "BILLING" not in r[0] and r[0] != "BACKROOM"]

    for zone_id in all_zone_ids:
        last_visit = (
            db.query(func.max(EventRecord.timestamp))
            .filter(
                EventRecord.store_id == store_id,
                EventRecord.zone_id == zone_id,
                EventRecord.event_type == "ZONE_ENTER",
            )
            .scalar()
        )
        if last_visit and last_visit < cutoff_dead:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type=AnomalyType.DEAD_ZONE,
                severity=AnomalySeverity.INFO,
                detected_at=now_str,
                description=f"Zone '{zone_id}' has had no visits in the last {DEAD_ZONE_MINUTES} minutes",
                suggested_action=f"Check merchandising in {zone_id}. Consider moving promotional display to this area.",
                zone_id=zone_id,
                metric_value=DEAD_ZONE_MINUTES,
                threshold_value=float(DEAD_ZONE_MINUTES),
            ))

    # ---- 3. Stale Feed ----
    cutoff_stale = (ref_now - timedelta(minutes=STALE_FEED_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_cameras = _find_stale_cameras(store_id, cutoff_stale, db)
    for cam_id, last_event_ts in stale_cameras:
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.STALE_FEED,
            severity=AnomalySeverity.WARN,
            detected_at=now_str,
            description=f"Camera {cam_id} last event was at {last_event_ts} — feed may be down",
            suggested_action=f"Check network connectivity for {cam_id}. Restart camera feed if needed.",
            zone_id=None,
            metric_value=None,
        ))

    # ---- 4. Conversion Drop ----
    conversion_anomaly = _detect_conversion_drop(store_id, ref_now, db)
    if conversion_anomaly:
        anomalies.append(conversion_anomaly)

    # ---- 5. Abandonment Spike ----
    abandonment_anomaly = _detect_abandonment_spike(store_id, db)
    if abandonment_anomaly:
        anomalies.append(abandonment_anomaly)

    # ---- 6. Empty Store ----
    window_30m = (ref_now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_entries = (
        db.query(EventRecord)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type.in_(["ENTRY", "ZONE_ENTER"]),
            EventRecord.is_staff == False,
            EventRecord.timestamp >= window_30m,
        )
        .count()
    )
    if recent_entries == 0:
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.EMPTY_STORE,
            severity=AnomalySeverity.INFO,
            detected_at=now_str,
            description="No customer activity detected in the last 30 minutes",
            suggested_action="Verify store is open and all camera feeds are operational.",
        ))

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies, checked_at=now_str)


def _find_stale_cameras(
    store_id: str, cutoff: str, db: Session
) -> List[tuple]:
    """Find cameras with no events since cutoff."""
    rows = (
        db.query(EventRecord.camera_id, func.max(EventRecord.timestamp))
        .filter(EventRecord.store_id == store_id)
        .group_by(EventRecord.camera_id)
        .all()
    )
    stale = [(r[0], r[1]) for r in rows if r[1] and r[1] < cutoff]
    return stale


def _detect_conversion_drop(
    store_id: str,
    ref_now: datetime,
    db: Session,
) -> Optional[Anomaly]:
    """Compare current conversion rate to 7-day avg (or full data baseline)."""
    # Use first half of data as baseline, second half as current
    all_entries = (
        db.query(EventRecord)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff == False,
        )
        .order_by(EventRecord.timestamp)
        .all()
    )
    if len(all_entries) < 10:
        return None

    midpoint_ts = all_entries[len(all_entries) // 2].timestamp
    
    def conversion_in_window(start: Optional[str], end: Optional[str]) -> float:
        q = db.query(EventRecord).filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
        )
        if start:
            q = q.filter(EventRecord.timestamp >= start)
        if end:
            q = q.filter(EventRecord.timestamp <= end)

        entries = set(r.visitor_id for r in q.filter(
            EventRecord.event_type == "ENTRY"
        ).with_entities(EventRecord.visitor_id).all())

        billing = set(r.visitor_id for r in q.filter(
            EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
            EventRecord.zone_id.in_(["BILLING", "BILLING_AREA", "BILLING_LEFT"]),
        ).with_entities(EventRecord.visitor_id).all()) & entries

        abandon = set(r.visitor_id for r in q.filter(
            EventRecord.event_type == "BILLING_QUEUE_ABANDON"
        ).with_entities(EventRecord.visitor_id).all())

        purchased = billing - abandon
        return len(purchased) / len(entries) if entries else 0.0

    baseline_rate = conversion_in_window(None, midpoint_ts)
    current_rate = conversion_in_window(midpoint_ts, None)

    if baseline_rate > 0 and current_rate < baseline_rate * (1 - CONVERSION_DROP_THRESHOLD):
        drop_pct = round((baseline_rate - current_rate) / baseline_rate * 100, 1)
        return Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.CONVERSION_DROP,
            severity=AnomalySeverity.WARN if drop_pct < 40 else AnomalySeverity.CRITICAL,
            detected_at=ref_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=f"Conversion rate dropped {drop_pct}% vs baseline ({current_rate:.1%} vs {baseline_rate:.1%})",
            suggested_action="Review customer journey in /funnel. Check for billing counter issues or queue buildup.",
            metric_value=round(current_rate, 4),
            threshold_value=round(baseline_rate * (1 - CONVERSION_DROP_THRESHOLD), 4),
        )
    return None


def _detect_abandonment_spike(store_id: str, db: Session) -> Optional[Anomaly]:
    """Detect if abandonment rate is unusually high."""
    joins = db.query(EventRecord).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_JOIN",
        EventRecord.is_staff == False,
    ).count()

    abandons = db.query(EventRecord).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_ABANDON",
        EventRecord.is_staff == False,
    ).count()

    if joins < 5:
        return None

    rate = abandons / joins
    if rate >= ABANDONMENT_SPIKE_THRESHOLD:
        return Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.ABANDONMENT_SPIKE,
            severity=AnomalySeverity.WARN,
            detected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=f"Billing abandonment rate is {rate:.1%} ({abandons}/{joins} customers left queue)",
            suggested_action="Reduce checkout time. Consider adding more billing staff or opening express lane.",
            zone_id="BILLING",
            metric_value=round(rate, 4),
            threshold_value=ABANDONMENT_SPIKE_THRESHOLD,
        )
    return None
