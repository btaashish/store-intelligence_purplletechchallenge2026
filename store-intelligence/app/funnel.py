"""
Conversion funnel computation: Entry → Zone Visit → Billing Queue → Purchase
Session-based, no double-counting of re-entries.
"""

import logging
from typing import Dict, List, Set
from sqlalchemy.orm import Session
from sqlalchemy import distinct

from .models import FunnelResponse, FunnelStage
from .core.database import EventRecord

logger = logging.getLogger(__name__)


def get_store_funnel(store_id: str, db: Session) -> FunnelResponse:
    """
    Compute conversion funnel. Visitor is the unit (not raw events).
    Re-entries: a visitor counts once per stage regardless of how many times they re-enter.
    """
    base = db.query(EventRecord).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
    )

    # Stage 1: Entered store (unique visitor_ids with ENTRY or REENTRY)
    entered: Set[str] = set(
        r.visitor_id for r in
        base.filter(EventRecord.event_type.in_(["ENTRY", "REENTRY"]))
        .with_entities(EventRecord.visitor_id).all()
    )

    # Stage 2: Visited at least one product zone (ZONE_ENTER, excluding entry/billing)
    product_zones = base.filter(
        EventRecord.event_type == "ZONE_ENTER",
        EventRecord.zone_id.notin_(["ENTRY_ZONE", "BACKROOM", "PURPLLE_MUM_1076_Z_BILLING_01", "BILLING", "BILLING_AREA", "BILLING_LEFT"]),
        EventRecord.zone_id.isnot(None),
        EventRecord.visitor_id.in_(list(entered)) if entered else False,
    ).with_entities(EventRecord.visitor_id).all()
    browsed: Set[str] = set(r.visitor_id for r in product_zones) & entered

    # Stage 3: Reached billing zone
    billing_visitors: Set[str] = set(
        r.visitor_id for r in
        base.filter(
            EventRecord.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
            EventRecord.zone_id.in_(["PURPLLE_MUM_1076_Z_BILLING_01", "BILLING", "BILLING_AREA", "BILLING_LEFT"]),
            EventRecord.visitor_id.in_(list(entered)) if entered else False,
        )
        .with_entities(EventRecord.visitor_id).all()
    ) & entered

    # Stage 4: Completed purchase (billing without subsequent abandon)
    abandoned: Set[str] = set(
        r.visitor_id for r in
        base.filter(EventRecord.event_type == "BILLING_QUEUE_ABANDON")
        .with_entities(EventRecord.visitor_id).all()
    )
    purchased: Set[str] = (billing_visitors - abandoned)

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 1)

    n_entered = len(entered)
    n_browsed = len(browsed)
    n_billing = len(billing_visitors)
    n_purchased = len(purchased)

    stages = [
        FunnelStage(stage="Store Entry", count=n_entered, drop_off_pct=0.0),
        FunnelStage(stage="Product Zone Visit",
                    count=n_browsed,
                    drop_off_pct=drop_off(n_entered, n_browsed)),
        FunnelStage(stage="Billing Queue",
                    count=n_billing,
                    drop_off_pct=drop_off(n_browsed, n_billing)),
        FunnelStage(stage="Purchase Completed",
                    count=n_purchased,
                    drop_off_pct=drop_off(n_billing, n_purchased)),
    ]

    conversion_rate = round(n_purchased / n_entered, 4) if n_entered > 0 else 0.0

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        conversion_rate=conversion_rate,
        session_count=n_entered,
    )
