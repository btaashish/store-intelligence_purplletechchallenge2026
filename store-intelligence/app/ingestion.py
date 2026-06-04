"""
Event ingestion: validate, deduplicate, and store events.
POST /events/ingest — idempotent by event_id.
"""

import logging
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import StoreEventIn, IngestRequest, IngestResponse
from .core.database import EventRecord

logger = logging.getLogger(__name__)


def ingest_events(
    events: List[StoreEventIn],
    db: Session,
) -> IngestResponse:
    """
    Ingest a batch of events. Idempotent: duplicate event_ids are counted but not re-inserted.
    Partial success: malformed events are rejected individually; others proceed.
    """
    accepted = 0
    rejected = 0
    duplicates = 0
    errors: List[Dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for event in events:
        try:
            record = EventRecord(
                event_id=event.event_id,
                store_id=event.store_id,
                camera_id=event.camera_id,
                visitor_id=event.visitor_id,
                event_type=event.event_type.value,
                timestamp=event.timestamp,
                zone_id=event.zone_id,
                dwell_ms=event.dwell_ms,
                is_staff=event.is_staff,
                confidence=event.confidence,
                queue_depth=event.metadata.queue_depth,
                sku_zone=event.metadata.sku_zone,
                session_seq=event.metadata.session_seq,
                ingested_at=now_utc,
            )
            db.add(record)
            db.flush()
            accepted += 1

        except IntegrityError:
            db.rollback()
            duplicates += 1
            logger.debug(f"Duplicate event_id: {event.event_id}")

        except Exception as e:
            db.rollback()
            rejected += 1
            errors.append({
                "event_id": getattr(event, "event_id", "unknown"),
                "error": str(e),
            })
            logger.warning(f"Failed to ingest event {getattr(event, 'event_id', '?')}: {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Commit failed: {e}")
        raise

    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicate=duplicates,
        errors=errors,
    )
