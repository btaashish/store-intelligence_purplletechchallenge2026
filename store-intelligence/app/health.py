"""Health endpoint - service status and feed staleness detection."""

import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from .models import HealthResponse
from .core.database import EventRecord

logger = logging.getLogger(__name__)

STALE_FEED_MINUTES = 10
_start_time = time.time()


def get_health(db: Session) -> HealthResponse:
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = (now_utc - timedelta(minutes=STALE_FEED_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    store_feeds: List[Dict[str, Any]] = []
    any_stale = False
    db_ok = True

    try:
        total_events = db.query(func.count(EventRecord.event_id)).scalar() or 0
        
        # Per-store, per-camera last event
        rows = (
            db.query(
                EventRecord.store_id,
                EventRecord.camera_id,
                func.max(EventRecord.timestamp).label("last_ts"),
            )
            .group_by(EventRecord.store_id, EventRecord.camera_id)
            .all()
        )

        store_map: Dict[str, Dict] = {}
        for r in rows:
            if r.store_id not in store_map:
                store_map[r.store_id] = {
                    "store_id": r.store_id,
                    "cameras": [],
                    "store_status": "OK",
                }
            
            # Compute lag relative to latest event in this store (not wall clock - video data)
            latest_in_store = (
                db.query(func.max(EventRecord.timestamp))
                .filter(EventRecord.store_id == r.store_id)
                .scalar()
            )
            
            try:
                last_ts_dt = datetime.strptime(r.last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                latest_dt = datetime.strptime(latest_in_store, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                lag_seconds = (latest_dt - last_ts_dt).total_seconds()
                cam_status = "STALE_FEED" if lag_seconds > STALE_FEED_MINUTES * 60 else "OK"
            except Exception:
                lag_seconds = None
                cam_status = "UNKNOWN"

            if cam_status == "STALE_FEED":
                any_stale = True
                store_map[r.store_id]["store_status"] = "STALE_FEED"

            store_map[r.store_id]["cameras"].append({
                "camera_id": r.camera_id,
                "last_event_ts": r.last_ts,
                "lag_seconds": round(lag_seconds, 1) if lag_seconds is not None else None,
                "status": cam_status,
            })

        store_feeds = list(store_map.values())

    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_ok = False
        total_events = 0

    if not db_ok:
        status = "unhealthy"
    elif any_stale:
        status = "degraded"
    else:
        status = "healthy"

    return HealthResponse(
        status=status,
        store_feeds=store_feeds,
        uptime_seconds=round(time.time() - _start_time, 1),
        total_events_ingested=total_events,
        checked_at=now_str,
    )
