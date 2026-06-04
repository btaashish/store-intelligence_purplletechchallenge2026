"""
emit.py — Event schema definition and emission utilities.

Every event emitted by the detection pipeline must conform to this schema.
Events are written as JSONL (one JSON object per line) to the output stream.
"""

import uuid
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

# Reference timestamps from video OSD overlays (visible in footage)
# Updated for Store ST1008 / Mumbai 1076 camera IDs
VIDEO_BASE_TIMES = {
    "cam1":                    datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc),   # entry cam
    "CAM2":                    datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc),   # left shelf
    "CAM3":                    datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc),   # centre shelf
    "CAM4":                    datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc),   # right shelf
    "PURPLLE_MUM_1076_CAM6":   datetime(2026, 3, 8, 18, 13, 0, tzinfo=timezone.utc),  # billing
}

# ─── Event Types ─────────────────────────────────────────────────────────────

class EventType:
    ENTRY                  = "ENTRY"
    EXIT                   = "EXIT"
    ZONE_ENTER             = "ZONE_ENTER"
    ZONE_EXIT              = "ZONE_EXIT"
    ZONE_DWELL             = "ZONE_DWELL"
    BILLING_QUEUE_JOIN     = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON  = "BILLING_QUEUE_ABANDON"
    REENTRY                = "REENTRY"


# ─── Event schema ─────────────────────────────────────────────────────────────

@dataclass
class StoreEvent:
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        # Clamp confidence to [0.0, 1.0]
        self.confidence = round(max(0.0, min(1.0, self.confidence)), 3)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metadata"].setdefault("queue_depth", None)
        d["metadata"].setdefault("sku_zone", None)
        d["metadata"].setdefault("session_seq", None)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ─── Visitor ID generation ────────────────────────────────────────────────────

def make_visitor_id(track_id: int) -> str:
    """Generate a short deterministic visitor token from track ID."""
    suffix = hex(hash(f"{track_id:04d}") & 0xFFFFFF)[2:].zfill(6)
    return f"VIS_{suffix}"


# ─── Timestamp helpers ────────────────────────────────────────────────────────

def frame_to_timestamp(clip_start_ts: datetime, frame_idx: int, fps: float) -> str:
    offset_sec = frame_idx / fps
    ts = clip_start_ts + timedelta(seconds=offset_sec)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── EventEmitter ─────────────────────────────────────────────────────────────

class EventEmitter:
    def __init__(self, store_id: str, camera_id: str, output_path: str,
                 clip_start_ts: Optional[datetime] = None, fps: float = 30.0):
        self.store_id = store_id
        self.camera_id = camera_id
        self.fps = fps
        self.clip_start_ts = clip_start_ts or VIDEO_BASE_TIMES.get(
            camera_id,
            datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc)
        )
        self._out = open(output_path, "a", buffering=1)
        self._session_seq: Dict[str, int] = defaultdict(int)
        self._zone_entry_frame: Dict[str, tuple] = {}
        self._dwell_last_emit: Dict[str, tuple] = {}
        self._billing_entry_frame: Dict[str, int] = {}
        self._total_emitted = 0

    def _ts(self, frame_idx: int) -> str:
        return frame_to_timestamp(self.clip_start_ts, frame_idx, self.fps)

    def _seq(self, visitor_id: str) -> int:
        self._session_seq[visitor_id] += 1
        return self._session_seq[visitor_id]

    def _emit(self, event: StoreEvent):
        self._out.write(event.to_json() + "\n")
        self._total_emitted += 1

    def on_entry(self, visitor_id: str, frame_idx: int, confidence: float,
                 is_staff: bool = False, is_reentry: bool = False):
        event_type = EventType.REENTRY if is_reentry else EventType.ENTRY
        self._emit(StoreEvent(
            store_id=self.store_id, camera_id=self.camera_id,
            visitor_id=visitor_id, event_type=event_type,
            timestamp=self._ts(frame_idx), zone_id=None, dwell_ms=0,
            is_staff=is_staff, confidence=round(confidence, 3),
            metadata={"queue_depth": None, "sku_zone": None, "session_seq": self._seq(visitor_id)},
        ))

    def on_exit(self, visitor_id: str, frame_idx: int, confidence: float,
                is_staff: bool = False):
        self._close_zone_dwell(visitor_id, frame_idx, confidence, is_staff)
        self._emit(StoreEvent(
            store_id=self.store_id, camera_id=self.camera_id,
            visitor_id=visitor_id, event_type=EventType.EXIT,
            timestamp=self._ts(frame_idx), zone_id=None, dwell_ms=0,
            is_staff=is_staff, confidence=round(confidence, 3),
            metadata={"queue_depth": None, "sku_zone": None, "session_seq": self._seq(visitor_id)},
        ))
        self._session_seq.pop(visitor_id, None)
        self._zone_entry_frame.pop(visitor_id, None)
        self._dwell_last_emit.pop(visitor_id, None)

    def on_zone_enter(self, visitor_id: str, zone_id: str, frame_idx: int,
                      confidence: float, is_staff: bool = False,
                      sku_zone: Optional[str] = None, queue_depth: Optional[int] = None):
        self._zone_entry_frame[visitor_id] = (zone_id, frame_idx)
        self._dwell_last_emit[visitor_id] = (zone_id, frame_idx)
        is_billing_queue = zone_id == "BILLING" and queue_depth and queue_depth > 0
        event_type = EventType.BILLING_QUEUE_JOIN if is_billing_queue else EventType.ZONE_ENTER
        if is_billing_queue:
            self._billing_entry_frame[visitor_id] = frame_idx
        self._emit(StoreEvent(
            store_id=self.store_id, camera_id=self.camera_id,
            visitor_id=visitor_id, event_type=event_type,
            timestamp=self._ts(frame_idx), zone_id=zone_id, dwell_ms=0,
            is_staff=is_staff, confidence=round(confidence, 3),
            metadata={"queue_depth": queue_depth, "sku_zone": sku_zone, "session_seq": self._seq(visitor_id)},
        ))

    def on_zone_exit(self, visitor_id: str, zone_id: str, frame_idx: int,
                     confidence: float, is_staff: bool = False,
                     sku_zone: Optional[str] = None, had_purchase: bool = False):
        entry_info = self._zone_entry_frame.pop(visitor_id, None)
        self._dwell_last_emit.pop(visitor_id, None)
        dwell_ms = 0
        if entry_info and entry_info[0] == zone_id:
            dwell_ms = int((frame_idx - entry_info[1]) / self.fps * 1000)
        is_abandon = (zone_id == "BILLING" and visitor_id in self._billing_entry_frame and not had_purchase)
        event_type = EventType.BILLING_QUEUE_ABANDON if is_abandon else EventType.ZONE_EXIT
        if is_abandon:
            self._billing_entry_frame.pop(visitor_id, None)
        self._emit(StoreEvent(
            store_id=self.store_id, camera_id=self.camera_id,
            visitor_id=visitor_id, event_type=event_type,
            timestamp=self._ts(frame_idx), zone_id=zone_id, dwell_ms=dwell_ms,
            is_staff=is_staff, confidence=round(confidence, 3),
            metadata={"queue_depth": None, "sku_zone": sku_zone, "session_seq": self._seq(visitor_id)},
        ))

    def on_dwell_tick(self, visitor_id: str, zone_id: str, frame_idx: int,
                      confidence: float, is_staff: bool = False,
                      sku_zone: Optional[str] = None):
        last = self._dwell_last_emit.get(visitor_id)
        if last is None or last[0] != zone_id:
            return
        dwell_ms = int((frame_idx - last[1]) / self.fps * 1000)
        if dwell_ms < 30_000:
            return
        self._dwell_last_emit[visitor_id] = (zone_id, frame_idx)
        self._emit(StoreEvent(
            store_id=self.store_id, camera_id=self.camera_id,
            visitor_id=visitor_id, event_type=EventType.ZONE_DWELL,
            timestamp=self._ts(frame_idx), zone_id=zone_id, dwell_ms=dwell_ms,
            is_staff=is_staff, confidence=round(confidence, 3),
            metadata={"queue_depth": None, "sku_zone": sku_zone, "session_seq": self._seq(visitor_id)},
        ))

    def _close_zone_dwell(self, visitor_id: str, frame_idx: int,
                          confidence: float, is_staff: bool):
        if visitor_id in self._zone_entry_frame:
            zone_id, entry_frame = self._zone_entry_frame[visitor_id]
            dwell_ms = int((frame_idx - entry_frame) / self.fps * 1000)
            self._emit(StoreEvent(
                store_id=self.store_id, camera_id=self.camera_id,
                visitor_id=visitor_id, event_type=EventType.ZONE_EXIT,
                timestamp=self._ts(frame_idx), zone_id=zone_id, dwell_ms=dwell_ms,
                is_staff=is_staff, confidence=round(confidence, 3),
                metadata={"queue_depth": None, "sku_zone": None, "session_seq": self._seq(visitor_id)},
            ))

    def flush(self):
        self._out.flush()

    def close(self):
        self._out.close()

    @property
    def total_emitted(self) -> int:
        return self._total_emitted
