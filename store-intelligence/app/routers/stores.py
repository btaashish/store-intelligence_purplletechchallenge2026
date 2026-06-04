"""
routers/stores.py — Store analytics endpoints.

GET /stores/{id}/metrics
GET /stores/{id}/funnel
GET /stores/{id}/heatmap
GET /stores/{id}/anomalies
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.models import StoreMetrics, StoreFunnel, StoreHeatmap, AnomalyResponse
from app.services.metrics import compute_store_metrics, compute_funnel, compute_heatmap
from app.services.anomalies import detect_anomalies
from app.database import get_events

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stores/{store_id}", tags=["stores"])


def _check_store(store_id: str):
    # For this challenge, we accept any store_id but warn if unknown
    known = {"ST1008", "store_1076", "ST1076", "STORE_BLR_002"}
    if store_id not in known:
        logger.warning("Request for unknown store_id: %s", store_id)


@router.get("/metrics", response_model=StoreMetrics)
async def get_metrics(
    store_id: str,
    start_ts: Optional[str] = Query(None, description="ISO-8601 UTC start (default: today)"),
    end_ts: Optional[str] = Query(None, description="ISO-8601 UTC end (default: now)"),
):
    """
    Real-time store metrics: unique visitors, conversion rate,
    avg dwell per zone, queue depth, abandonment rate.

    Excludes is_staff=true events. Handles zero-purchase stores gracefully.
    """
    _check_store(store_id)
    try:
        data = await compute_store_metrics(store_id, start_ts, end_ts)
        return StoreMetrics(**data)
    except Exception as e:
        logger.exception("metrics computation failed store=%s", store_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/funnel", response_model=StoreFunnel)
async def get_funnel(
    store_id: str,
    start_ts: Optional[str] = Query(None),
    end_ts: Optional[str] = Query(None),
):
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Session-based, re-entries do not double-count visitors.
    """
    _check_store(store_id)
    try:
        data = await compute_funnel(store_id, start_ts, end_ts)
        return StoreFunnel(**data)
    except Exception as e:
        logger.exception("funnel computation failed store=%s", store_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/heatmap", response_model=StoreHeatmap)
async def get_heatmap(
    store_id: str,
    start_ts: Optional[str] = Query(None),
    end_ts: Optional[str] = Query(None),
):
    """
    Zone visit frequency + avg dwell normalised 0–100.
    Includes data_confidence flag if fewer than 20 sessions.
    """
    _check_store(store_id)
    try:
        data = await compute_heatmap(store_id, start_ts, end_ts)
        return StoreHeatmap(**data)
    except Exception as e:
        logger.exception("heatmap computation failed store=%s", store_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/anomalies", response_model=AnomalyResponse)
async def get_anomalies(store_id: str):
    """
    Active anomalies: queue spike, conversion drop, dead zones, high abandonment.
    Each anomaly has severity (INFO/WARN/CRITICAL) and suggested_action.
    """
    _check_store(store_id)
    try:
        from datetime import datetime, timezone
        anomalies = await detect_anomalies(store_id)
        return AnomalyResponse(
            store_id=store_id,
            active_anomalies=anomalies,
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except Exception as e:
        logger.exception("anomaly detection failed store=%s", store_id)
        raise HTTPException(status_code=500, detail=str(e))
