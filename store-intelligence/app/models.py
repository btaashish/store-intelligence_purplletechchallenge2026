"""Pydantic models for the Store Intelligence API."""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Any, Dict
from datetime import datetime
from enum import Enum
import uuid


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEventIn(BaseModel):
    event_id: str = Field(..., description="UUID v4 - must be globally unique")
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @validator("timestamp")
    def validate_timestamp(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError("timestamp must be ISO-8601 UTC format: YYYY-MM-DDTHH:MM:SSZ")
        return v

    @validator("event_id")
    def validate_event_id(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("event_id must be a valid UUID")
        return v


class IngestRequest(BaseModel):
    events: List[StoreEventIn] = Field(..., max_items=500)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class ZoneDwellMetric(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_count: int
    avg_dwell_ms: float
    total_dwell_ms: float


class StoreMetrics(BaseModel):
    store_id: str
    window_start: str
    window_end: str
    unique_visitors: int
    total_entries: int
    conversion_rate: float
    avg_dwell_ms: float
    queue_depth_current: int
    abandonment_rate: float
    zone_metrics: List[ZoneDwellMetric]
    data_confidence: str = "HIGH"  # HIGH / MEDIUM / LOW


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: List[FunnelStage]
    conversion_rate: float
    session_count: int


class HeatmapZone(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_count: int
    avg_dwell_ms: float
    normalized_score: float  # 0-100
    data_confidence: str = "HIGH"


class HeatmapResponse(BaseModel):
    store_id: str
    zones: List[HeatmapZone]
    generated_at: str


class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"
    EMPTY_STORE = "EMPTY_STORE"
    ABANDONMENT_SPIKE = "ABANDONMENT_SPIKE"


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    detected_at: str
    description: str
    suggested_action: str
    zone_id: Optional[str] = None
    metric_value: Optional[float] = None
    threshold_value: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: List[Anomaly]
    checked_at: str


class CameraHealth(BaseModel):
    camera_id: str
    last_event_ts: Optional[str]
    lag_seconds: Optional[float]
    status: str  # OK / STALE_FEED / NO_DATA


class HealthResponse(BaseModel):
    status: str  # healthy / degraded / unhealthy
    store_feeds: List[Dict[str, Any]]
    uptime_seconds: float
    total_events_ingested: int
    checked_at: str
