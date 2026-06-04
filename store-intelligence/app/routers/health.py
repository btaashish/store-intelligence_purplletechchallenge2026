"""
routers/health.py — GET /health endpoint.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database import get_last_event_time, count_total_events

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Module-level start time (avoids circular import from app.main)
_START_TIME = time.time()
VERSION = "1.0.0"
STORE_ID = "ST1008"
CAMERAS = ["cam1", "CAM2", "CAM3", "CAM4", "PURPLLE_MUM_1076_CAM6"]
STALE_MINUTES = 10


@router.get("/health")
async def health():
    """Service health check: DB status, feed freshness, uptime."""
    now = datetime.now(timezone.utc)
    db_status = "connected"
    total_events = 0

    try:
        total_events = await count_total_events()
    except Exception as e:
        logger.error("DB health check failed: %s", e)
        return JSONResponse(status_code=503, content={
            "status": "unhealthy",
            "db_status": "unavailable",
            "error": str(e),
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    feeds = []
    has_stale = False
    for cam_id in CAMERAS:
        last_ts = await get_last_event_time(STORE_ID, cam_id)
        if last_ts is None:
            feeds.append({"camera_id": cam_id, "last_event_at": None,
                          "lag_minutes": None, "status": "NO_DATA"})
            continue
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        lag_min = (now - last_dt).total_seconds() / 60
        status = "STALE_FEED" if lag_min > STALE_MINUTES else "OK"
        if status == "STALE_FEED":
            has_stale = True
        feeds.append({"camera_id": cam_id, "last_event_at": last_ts,
                      "lag_minutes": round(lag_min, 1), "status": status})

    return {
        "status": "degraded" if has_stale else "healthy",
        "store_feeds": feeds,
        "db_status": db_status,
        "total_events_stored": total_events,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "version": VERSION,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
