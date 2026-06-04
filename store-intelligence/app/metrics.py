"""
Real-time metric computation for /stores/{id}/metrics endpoint.
All metrics computed from live DB state - never cached from yesterday.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct, text

from .models import StoreMetrics, ZoneDwellMetric
from .core.database import EventRecord

logger = logging.getLogger(__name__)

# Time window for "today" — use last 24h from latest event in store
WINDOW_HOURS = 24

# Minimum sessions for HIGH confidence
MIN_SESSIONS_HIGH_CONF = 20


def get_store_metrics(store_id: str, db: Session) -> StoreMetrics:
    """
    Compute real-time store metrics for store_id.
    Handles zero-purchase stores and zero-traffic periods gracefully.
    """
    now = datetime.now(timezone.utc)

    # Determine window: use latest event timestamp as "now" for video data
    latest_ts_row = (
        db.query(func.max(EventRecord.timestamp))
        .filter(EventRecord.store_id == store_id)
        .scalar()
    )
    if latest_ts_row:
        try:
            latest_ts = datetime.strptime(latest_ts_row, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            latest_ts = now
    else:
        latest_ts = now

    window_end = latest_ts
    window_start = window_end - timedelta(hours=WINDOW_HOURS)
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end_str = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Base query filter
    base = db.query(EventRecord).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.timestamp >= window_start_str,
        EventRecord.timestamp <= window_end_str,
    )

    # Unique visitors (ENTRY events, deduplicated by visitor_id)
    unique_visitors = (
        base.filter(EventRecord.event_type.in_(["ENTRY", "REENTRY"]))
        .with_entities(distinct(EventRecord.visitor_id))
        .count()
    )

    # Total ENTRY events
    total_entries = base.filter(EventRecord.event_type == "ENTRY").count()

    # Visitors who reached billing (proxy for conversion since no customer_id in POS)
    # A visitor "converted" if they had a BILLING_QUEUE_JOIN without BILLING_QUEUE_ABANDON
    billing_joins = set(
        r.visitor_id for r in
        base.filter(EventRecord.event_type == "BILLING_QUEUE_JOIN")
        .with_entities(EventRecord.visitor_id).all()
    )
    billing_abandons = set(
        r.visitor_id for r in
        base.filter(EventRecord.event_type == "BILLING_QUEUE_ABANDON")
        .with_entities(EventRecord.visitor_id).all()
    )
    # Also include visitors in billing zone (PURPLLE_MUM_1076_CAM6 ZONE_ENTER for PURPLLE_MUM_1076_Z_BILLING_01)
    billing_zone_visitors = set(
        r.visitor_id for r in
        base.filter(
            EventRecord.event_type == "ZONE_ENTER",
            EventRecord.zone_id.in_(["PURPLLE_MUM_1076_Z_BILLING_01", "BILLING", "BILLING_AREA", "BILLING_LEFT"])
        )
        .with_entities(EventRecord.visitor_id).all()
    )
    all_billing_visitors = billing_joins | billing_zone_visitors
    converted_visitors = all_billing_visitors - billing_abandons

    conversion_rate = 0.0
    if unique_visitors > 0:
        conversion_rate = round(len(converted_visitors) / unique_visitors, 4)

    # Abandonment rate
    abandonment_rate = 0.0
    if billing_joins:
        abandonment_rate = round(len(billing_abandons & billing_joins) / len(billing_joins), 4)

    # Average dwell time (from ZONE_EXIT events which carry dwell_ms)
    dwell_rows = (
        base.filter(
            EventRecord.event_type == "ZONE_EXIT",
            EventRecord.dwell_ms > 0,
        )
        .with_entities(EventRecord.dwell_ms)
        .all()
    )
    avg_dwell_ms = 0.0
    if dwell_rows:
        avg_dwell_ms = sum(r.dwell_ms for r in dwell_rows) / len(dwell_rows)

    # Current queue depth (latest BILLING_QUEUE_JOIN queue_depth)
    latest_queue = (
        db.query(EventRecord)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.queue_depth.isnot(None),
        )
        .order_by(EventRecord.timestamp.desc())
        .first()
    )
    queue_depth_current = latest_queue.queue_depth if latest_queue else 0

    # Zone metrics
    zone_metrics = _compute_zone_metrics(store_id, window_start_str, window_end_str, db)

    # Data confidence
    data_confidence = "HIGH" if unique_visitors >= MIN_SESSIONS_HIGH_CONF else (
        "MEDIUM" if unique_visitors >= 5 else "LOW"
    )

    return StoreMetrics(
        store_id=store_id,
        window_start=window_start_str,
        window_end=window_end_str,
        unique_visitors=unique_visitors,
        total_entries=total_entries,
        conversion_rate=conversion_rate,
        avg_dwell_ms=round(avg_dwell_ms, 1),
        queue_depth_current=queue_depth_current,
        abandonment_rate=abandonment_rate,
        zone_metrics=zone_metrics,
        data_confidence=data_confidence,
    )


def _compute_zone_metrics(
    store_id: str,
    window_start: str,
    window_end: str,
    db: Session,
) -> List[ZoneDwellMetric]:
    """Compute per-zone visit counts and dwell times."""
    rows = (
        db.query(
            EventRecord.zone_id,
            EventRecord.sku_zone,
            func.count(EventRecord.visitor_id).label("visit_count"),
            func.avg(EventRecord.dwell_ms).label("avg_dwell"),
            func.sum(EventRecord.dwell_ms).label("total_dwell"),
        )
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.event_type == "ZONE_EXIT",
            EventRecord.zone_id.isnot(None),
            EventRecord.timestamp >= window_start,
            EventRecord.timestamp <= window_end,
        )
        .group_by(EventRecord.zone_id)
        .all()
    )

    return [
        ZoneDwellMetric(
            zone_id=r.zone_id,
            sku_zone=r.sku_zone,
            visit_count=r.visit_count,
            avg_dwell_ms=round(r.avg_dwell or 0, 1),
            total_dwell_ms=r.total_dwell or 0,
        )
        for r in rows
        if r.zone_id and "BILLING" not in (r.zone_id or "").upper()
    ]
