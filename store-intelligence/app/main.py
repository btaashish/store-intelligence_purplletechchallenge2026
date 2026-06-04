"""
Store Intelligence API - FastAPI application entry point.
Production-aware: structured logging, trace IDs, graceful degradation, idempotency.
"""

import csv
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from .core.database import create_tables, get_db
from .models import (
    IngestRequest, IngestResponse,
    StoreMetrics, FunnelResponse, HeatmapResponse, HeatmapZone,
    AnomaliesResponse, HealthResponse,
)
from .ingestion import ingest_events
from .metrics import get_store_metrics
from .funnel import get_store_funnel
from .anomalies import get_anomalies
from .health import get_health

# ---- Structured logging setup ----
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown lifecycle."""
    logger.info('{"event": "startup", "service": "store-intelligence-api"}')
    try:
        create_tables()
        logger.info('{"event": "db_tables_ready"}')
    except Exception as e:
        logger.error(f'{{"event": "db_init_failed", "error": "{e}"}}')

    # Load POS CSV on startup if provided
    pos_csv = os.environ.get("POS_CSV_PATH", "")
    if pos_csv and os.path.exists(pos_csv):
        try:
            _load_pos_csv(pos_csv)
            logger.info(f'{{"event": "pos_csv_loaded", "path": "{pos_csv}"}}')
        except Exception as e:
            logger.warning(f'{{"event": "pos_csv_load_failed", "error": "{e}"}}')
    else:
        logger.info('{"event": "pos_csv_skip", "reason": "POS_CSV_PATH not set or file missing"}')

    yield
    logger.info('{"event": "shutdown"}')


def _load_pos_csv(path: str) -> int:
    """
    Load POS transactions from CSV into the DB.
    Handles the actual CSV schema:
      order_id, order_date, order_time, store_id, product_id, brand_name, total_amount

    Groups rows by (store_id, order_date, order_time) → one transaction per checkout.
    transaction_id = synthetic TXN_{order_id} per row (each line-item is its own record
    so we can track basket value per item; conversion correlation uses the timestamp).
    """
    from .database import get_db_sync, insert_transactions_sync
    inserted = 0
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Parse date + time → ISO-8601 UTC timestamp
                date_str = row["order_date"].strip()   # e.g. 10-04-2026
                time_str = row["order_time"].strip()   # e.g. 12:15:05
                dt = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S")
                timestamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                txn = {
                    "transaction_id": f"TXN_{row['order_id'].strip()}",
                    "store_id":       row["store_id"].strip(),
                    "timestamp":      timestamp,
                    "basket_value":   float(row["total_amount"]),
                }
                rows.append(txn)
            except (KeyError, ValueError) as e:
                logger.warning(f'{{"event":"pos_row_skip","error":"{e}","row":"{row}"}}')

    if rows:
        inserted = insert_transactions_sync(rows)
    logger.info(f'{{"event":"pos_csv_rows_loaded","count":{inserted}}}')
    return inserted


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV event streams",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Request logging middleware ----
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    request.state.trace_id = trace_id
    start = time.time()

    try:
        response = await call_next(request)
        latency_ms = round((time.time() - start) * 1000, 1)
        store_id = request.path_params.get("store_id", "-")
        logger.info(
            f'{{"trace_id":"{trace_id}","method":"{request.method}",'
            f'"path":"{request.url.path}","store_id":"{store_id}",'
            f'"status_code":{response.status_code},"latency_ms":{latency_ms}}}'
        )
        response.headers["X-Trace-ID"] = trace_id
        return response
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000, 1)
        logger.error(f'{{"trace_id":"{trace_id}","error":"{e}","latency_ms":{latency_ms}}}')
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "trace_id": trace_id},
        )


def _db_error_response(e: Exception, trace_id: str = "") -> JSONResponse:
    """Return 503 with structured body on DB failure. Never expose stack traces."""
    logger.error(f'{{"event":"db_error","error":"{type(e).__name__}","trace_id":"{trace_id}"}}')
    return JSONResponse(
        status_code=503,
        content={
            "error": "Service temporarily unavailable - database error",
            "code": "DB_UNAVAILABLE",
            "trace_id": trace_id,
        },
    )


# ===================================================================
# POST /events/ingest
# ===================================================================
@app.post("/events/ingest", response_model=IngestResponse, status_code=200)
async def ingest(
    request: Request,
    payload: IngestRequest,
    db: Session = Depends(get_db),
):
    """
    Ingest batch of events (max 500). Idempotent by event_id.
    Returns partial success on malformed events.
    """
    trace_id = getattr(request.state, "trace_id", "")
    try:
        result = ingest_events(payload.events, db)
        logger.info(
            f'{{"trace_id":"{trace_id}","event":"ingest_complete",'
            f'"accepted":{result.accepted},"rejected":{result.rejected},'
            f'"duplicate":{result.duplicate},"count":{len(payload.events)}}}'
        )
        return result
    except OperationalError as e:
        return _db_error_response(e, trace_id)


# ===================================================================
# GET /stores/{id}/metrics
# ===================================================================
@app.get("/stores/{store_id}/metrics", response_model=StoreMetrics)
async def metrics(
    request: Request,
    store_id: str,
    db: Session = Depends(get_db),
):
    """Real-time store metrics: visitors, conversion, dwell, queue depth."""
    trace_id = getattr(request.state, "trace_id", "")
    try:
        return get_store_metrics(store_id, db)
    except OperationalError as e:
        return _db_error_response(e, trace_id)


# ===================================================================
# GET /stores/{id}/funnel
# ===================================================================
@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def funnel(
    request: Request,
    store_id: str,
    db: Session = Depends(get_db),
):
    """Conversion funnel: Entry → Browse → Billing → Purchase."""
    trace_id = getattr(request.state, "trace_id", "")
    try:
        return get_store_funnel(store_id, db)
    except OperationalError as e:
        return _db_error_response(e, trace_id)


# ===================================================================
# GET /stores/{id}/heatmap
# ===================================================================
@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def heatmap(
    request: Request,
    store_id: str,
    db: Session = Depends(get_db),
):
    """Zone visit frequency + avg dwell, normalized 0-100."""
    trace_id = getattr(request.state, "trace_id", "")
    from datetime import datetime, timezone
    from sqlalchemy import func, distinct
    from .core.database import EventRecord

    try:
        rows = (
            db.query(
                EventRecord.zone_id,
                EventRecord.sku_zone,
                func.count(EventRecord.visitor_id).label("visit_count"),
                func.avg(EventRecord.dwell_ms).label("avg_dwell"),
            )
            .filter(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.event_type == "ZONE_EXIT",
                EventRecord.zone_id.isnot(None),
            )
            .group_by(EventRecord.zone_id)
            .all()
        )

        if not rows:
            return HeatmapResponse(
                store_id=store_id,
                zones=[],
                generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

        max_visits = max(r.visit_count for r in rows) or 1
        total_sessions = (
            db.query(func.count(distinct(EventRecord.visitor_id)))
            .filter(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
            )
            .scalar() or 1
        )

        zones = []
        for r in rows:
            if not r.zone_id or "BILLING" in (r.zone_id or "").upper():
                continue
            norm = round(r.visit_count / max_visits * 100, 1)
            confidence = "HIGH" if total_sessions >= 20 else ("MEDIUM" if total_sessions >= 5 else "LOW")
            zones.append(HeatmapZone(
                zone_id=r.zone_id,
                sku_zone=r.sku_zone,
                visit_count=r.visit_count,
                avg_dwell_ms=round(r.avg_dwell or 0, 1),
                normalized_score=norm,
                data_confidence=confidence,
            ))

        zones.sort(key=lambda z: z.normalized_score, reverse=True)

        return HeatmapResponse(
            store_id=store_id,
            zones=zones,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except OperationalError as e:
        return _db_error_response(e, trace_id)


# ===================================================================
# GET /stores/{id}/anomalies
# ===================================================================
@app.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def anomalies(
    request: Request,
    store_id: str,
    db: Session = Depends(get_db),
):
    """Active anomalies: queue spike, conversion drop, dead zone."""
    trace_id = getattr(request.state, "trace_id", "")
    try:
        return get_anomalies(store_id, db)
    except OperationalError as e:
        return _db_error_response(e, trace_id)


# ===================================================================
# GET /health
# ===================================================================
@app.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    db: Session = Depends(get_db),
):
    """Service health: uptime, event counts, feed staleness."""
    try:
        return get_health(db)
    except OperationalError as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": "database unavailable"},
        )


@app.get("/")
async def root():
    return {"service": "Store Intelligence API", "version": "1.0.0", "docs": "/docs"}
