"""
routers/events.py — POST /events/ingest endpoint.
"""

import logging
from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, Request
from app.models import IngestRequest, IngestResponse, StoreEventIn
from app.database import insert_events_batch

logger = logging.getLogger(__name__)
router = APIRouter(tags=["events"])


@router.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(request: Request, payload: IngestRequest):
    """
    Ingest a batch of up to 500 detection events.

    Idempotent by event_id — safe to call twice with same payload.
    Returns partial success on malformed events.
    """
    if not payload.events:
        return IngestResponse(accepted=0, rejected=0, duplicate=0)

    # Validate and convert
    valid_events: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for evt in payload.events:
        try:
            valid_events.append(evt.model_dump())
        except Exception as e:
            errors.append({"event_id": getattr(evt, "event_id", "unknown"), "error": str(e)})

    if not valid_events:
        raise HTTPException(status_code=422, detail={"message": "All events failed validation", "errors": errors})

    counts = await insert_events_batch(valid_events)

    return IngestResponse(
        accepted=counts["accepted"],
        rejected=counts["rejected"] + len(errors),
        duplicate=counts["duplicate"],
        errors=errors,
    )
