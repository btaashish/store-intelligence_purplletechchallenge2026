"""
detect.py — Main detection + zone logic for a single CCTV clip.

Camera roles (Store ST1008 / Mumbai 1076):
    cam1                  → Entry/Exit threshold (glass door)
    CAM2                  → Left Shelf zone (PURPLLE_MUM_1076_Z01)
    CAM3                  → Centre Shelf zone (PURPLLE_MUM_1076_Z02)
    CAM4                  → Right Shelf zone (PURPLLE_MUM_1076_Z03)
    PURPLLE_MUM_1076_CAM6 → Billing Counter Queue (PURPLLE_MUM_1076_Z_BILLING_01)
"""

import cv2
import logging
import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Set, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from tracker import PersonTracker, KalmanTrack, DETECT_RESIZE_W, DETECT_RESIZE_H
from emit import EventEmitter, make_visitor_id, EventType, VIDEO_BASE_TIMES

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("detect")

CAMERA_META = {
    "cam1": {
        "camera_id": "cam1",
        "zones": ["ENTRY_ZONE"],
        "default_zone": "ENTRY_ZONE",
        "sku_zone": None,
        "staff_only": False, "is_billing": False, "is_entry": True,
    },
    "CAM2": {
        "camera_id": "CAM2",
        "zones": ["PURPLLE_MUM_1076_Z01"],
        "default_zone": "PURPLLE_MUM_1076_Z01",
        "sku_zone": "LEFT_SHELF",
        "staff_only": False, "is_billing": False, "is_entry": False,
    },
    "CAM3": {
        "camera_id": "CAM3",
        "zones": ["PURPLLE_MUM_1076_Z02"],
        "default_zone": "PURPLLE_MUM_1076_Z02",
        "sku_zone": "CENTRE_SHELF",
        "staff_only": False, "is_billing": False, "is_entry": False,
    },
    "CAM4": {
        "camera_id": "CAM4",
        "zones": ["PURPLLE_MUM_1076_Z03"],
        "default_zone": "PURPLLE_MUM_1076_Z03",
        "sku_zone": "RIGHT_SHELF",
        "staff_only": False, "is_billing": False, "is_entry": False,
    },
    "PURPLLE_MUM_1076_CAM6": {
        "camera_id": "PURPLLE_MUM_1076_CAM6",
        "zones": ["PURPLLE_MUM_1076_Z_BILLING_01"],
        "default_zone": "PURPLLE_MUM_1076_Z_BILLING_01",
        "sku_zone": None,
        "staff_only": False, "is_billing": True, "is_entry": False,
    },
}


class EntryExitDetector:
    """
    Direction-based entry/exit detection for cam1 (glass door entry camera).
    Persons crossing below the threshold line = entering.
    Track disappearing near exit region (high x-ratio) = EXIT.
    """
    EXIT_X_THRESHOLD = 0.70

    def __init__(self, frame_w: int, scale_w: float = 1.0):
        self.frame_w = frame_w
        self.scale_w = scale_w
        self._last_bbox: Dict[int, object] = {}

    def update_pos(self, tid: int, bbox):
        self._last_bbox[tid] = bbox

    def on_track_lost(self, tid: int, frame_w: int) -> Optional[str]:
        bbox = self._last_bbox.pop(tid, None)
        if bbox is None:
            return None
        cx = (float(bbox[0]) + float(bbox[2]) / 2) / frame_w
        if cx > self.EXIT_X_THRESHOLD:
            return EventType.EXIT
        return None

    def remove(self, tid: int):
        self._last_bbox.pop(tid, None)


class ZoneAssigner:
    def __init__(self, camera_key: str, frame_w: int):
        self.camera_key = camera_key
        self.meta = CAMERA_META.get(camera_key, {})
        self.frame_w = frame_w

    def assign(self, bbox) -> str:
        zones = self.meta.get("zones", [])
        default = self.meta.get("default_zone", "UNKNOWN")
        # All current cameras cover a single zone — no horizontal split needed
        return default


def process_video(video_path: str, camera_key: str, output_path: str,
                  store_id: str = "ST1008", realtime_delay: float = 0.0,
                  max_frames: Optional[int] = None) -> int:
    meta = CAMERA_META[camera_key]
    camera_id = meta["camera_id"]
    is_entry_cam = meta["is_entry"]
    is_billing_cam = meta["is_billing"]
    is_staff_only = meta["staff_only"]

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    scale_w = frame_w / DETECT_RESIZE_W
    scale_h = frame_h / DETECT_RESIZE_H

    clip_start = VIDEO_BASE_TIMES.get(camera_key,
        datetime(2026, 3, 8, 18, 10, 0, tzinfo=timezone.utc))
    logger.info(f"Processing {camera_key}: {frame_w}x{frame_h}@{fps:.0f}fps {total_frames}f")

    tracker = PersonTracker(camera_key, scale_w=scale_w, scale_h=scale_h)
    emitter = EventEmitter(store_id, camera_id, output_path, clip_start, fps)
    zone_assigner = ZoneAssigner(camera_key, frame_w)
    entry_exit = EntryExitDetector(frame_w, scale_w) if is_entry_cam else None

    active_visitors: Dict[int, str] = {}
    visitor_zone: Dict[str, str] = {}
    visitor_entered: Set[str] = set()
    prev_ids: Set[int] = set()
    queue_depth = 0
    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret or (max_frames and frame_idx >= max_frames):
                break

            confirmed = tracker.update(frame)
            cur_ids = set(confirmed.keys())

            for tid in cur_ids - prev_ids:
                t = confirmed[tid]
                vid = make_visitor_id(tid)
                active_visitors[tid] = vid

                if is_entry_cam:
                    entry_exit.update_pos(tid, t.bbox)
                    if vid not in visitor_entered:
                        visitor_entered.add(vid)
                        emitter.on_entry(vid, frame_idx,
                            confidence=min(1.0, t.hits/3.0),
                            is_staff=t.is_staff)
                else:
                    if vid not in visitor_entered:
                        visitor_entered.add(vid)
                        emitter.on_entry(vid, frame_idx,
                            confidence=min(1.0, t.hits/3.0),
                            is_staff=t.is_staff or is_staff_only)
                    zone = zone_assigner.assign(t.bbox)
                    visitor_zone[vid] = zone
                    qd = queue_depth if is_billing_cam else None
                    emitter.on_zone_enter(vid, zone, frame_idx,
                        confidence=min(1.0, t.hits/3.0),
                        is_staff=t.is_staff or is_staff_only,
                        sku_zone=meta.get("sku_zone"), queue_depth=qd)

            if is_entry_cam and entry_exit:
                for tid in cur_ids:
                    if tid in confirmed:
                        entry_exit.update_pos(tid, confirmed[tid].bbox)

            if not is_entry_cam and not is_billing_cam:
                for tid, t in confirmed.items():
                    vid = active_visitors.get(tid)
                    if not vid:
                        continue
                    new_zone = zone_assigner.assign(t.bbox)
                    old_zone = visitor_zone.get(vid)
                    if old_zone and old_zone != new_zone:
                        emitter.on_zone_exit(vid, old_zone, frame_idx,
                            confidence=min(1.0, t.hits/3.0), is_staff=t.is_staff,
                            sku_zone=meta.get("sku_zone"))
                        visitor_zone[vid] = new_zone
                        emitter.on_zone_enter(vid, new_zone, frame_idx,
                            confidence=min(1.0, t.hits/3.0), is_staff=t.is_staff,
                            sku_zone=meta.get("sku_zone"))

            if is_billing_cam:
                customer_count = sum(1 for t in confirmed.values() if not t.is_staff)
                queue_depth = customer_count

            dwell_interval = int(fps * 30)
            if frame_idx > 0 and dwell_interval > 0 and frame_idx % dwell_interval == 0:
                for tid, t in confirmed.items():
                    vid = active_visitors.get(tid)
                    if not vid:
                        continue
                    zone = visitor_zone.get(vid) or meta.get("default_zone")
                    if zone:
                        emitter.on_dwell_tick(vid, zone, frame_idx,
                            confidence=min(1.0, t.hits/3.0),
                            is_staff=t.is_staff or is_staff_only,
                            sku_zone=meta.get("sku_zone"))

            for tid in prev_ids - cur_ids:
                vid = active_visitors.pop(tid, None)
                if not vid:
                    continue
                if is_entry_cam and entry_exit:
                    direction = entry_exit.on_track_lost(tid, frame_w)
                    if direction == EventType.EXIT and vid in visitor_entered:
                        emitter.on_exit(vid, frame_idx, confidence=0.5, is_staff=False)
                    else:
                        entry_exit.remove(tid)
                else:
                    zone = visitor_zone.pop(vid, None)
                    if zone:
                        emitter.on_zone_exit(vid, zone, frame_idx,
                            confidence=0.5, is_staff=is_staff_only)
                    emitter.on_exit(vid, frame_idx, confidence=0.5,
                                    is_staff=is_staff_only)

            prev_ids = cur_ids
            frame_idx += 1
            if realtime_delay > 0:
                time.sleep(realtime_delay)
            if frame_idx % 300 == 0:
                logger.info(f"{camera_key} frame {frame_idx}/{total_frames} "
                            f"tracks={len(confirmed)} events={emitter.total_emitted}")
    finally:
        cap.release()
        emitter.flush()
        emitter.close()

    logger.info(f"Done {camera_key}: {frame_idx} frames, {emitter.total_emitted} events → {output_path}")
    return emitter.total_emitted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--camera-id", required=True, choices=list(CAMERA_META.keys()))
    parser.add_argument("--output", default="events.jsonl")
    parser.add_argument("--store-id", default="ST1008")
    parser.add_argument("--realtime", type=float, default=0.0)
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()
    n = process_video(args.video, args.camera_id, args.output,
                      args.store_id, args.realtime, args.max_frames)
    print(f"Emitted {n} events → {args.output}")
