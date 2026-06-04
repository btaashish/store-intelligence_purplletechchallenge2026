"""
Zone classifier: maps (x, y) centroid coordinates to named store zones.
Updated for Store ST1008 / Mumbai 1076 layout.

Camera-to-zone mapping based on store_layout.json and footage inspection.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import json


@dataclass
class Zone:
    zone_id: str
    name: str
    sku_zone: str
    camera_id: str
    # Bounding box in normalized coords (0-1) relative to frame
    x_min: float
    y_min: float
    x_max: float
    y_max: float


STORE_ID   = "ST1008"
STORE_NAME = "Purplle_Mumbai_1076"

# Each camera covers a single zone; full-frame bounding boxes are used.
# For billing cam, the queue area occupies the lower half of the frame.
CAMERA_ZONE_MAP: Dict[str, List[Zone]] = {
    "cam1": [
        Zone("ENTRY_ZONE", "Entry / Exit", "ENTRY", "cam1", 0.0, 0.0, 1.0, 1.0),
    ],
    "CAM2": [
        Zone("PURPLLE_MUM_1076_Z01", "Left Shelf", "LEFT_SHELF", "CAM2", 0.0, 0.0, 1.0, 1.0),
    ],
    "CAM3": [
        Zone("PURPLLE_MUM_1076_Z02", "Centre Shelf", "CENTRE_SHELF", "CAM3", 0.0, 0.0, 1.0, 1.0),
    ],
    "CAM4": [
        Zone("PURPLLE_MUM_1076_Z03", "Right Shelf", "RIGHT_SHELF", "CAM4", 0.0, 0.0, 1.0, 1.0),
    ],
    "PURPLLE_MUM_1076_CAM6": [
        Zone("PURPLLE_MUM_1076_Z_BILLING_01", "Billing Counter Queue",
             "BILLING", "PURPLLE_MUM_1076_CAM6", 0.0, 0.0, 1.0, 1.0),
    ],
}

ENTRY_CAMERA            = "cam1"
ENTRY_LINE_Y_NORMALIZED = 0.6
BILLING_CAMERAS         = ["PURPLLE_MUM_1076_CAM6"]
STAFF_CAMERA            = None   # no dedicated staff/backroom camera in this store


class ZoneClassifier:
    def __init__(self, frame_width: int = 1920, frame_height: int = 1080):
        self.frame_width  = frame_width
        self.frame_height = frame_height

    def classify(self, cx: int, cy: int, camera_id: str) -> Optional[Zone]:
        """Return the zone for a centroid (cx, cy) in pixel coordinates."""
        zones = CAMERA_ZONE_MAP.get(camera_id, [])
        nx = cx / self.frame_width
        ny = cy / self.frame_height

        best: Optional[Zone] = None
        best_area = float("inf")
        for zone in zones:
            if zone.x_min <= nx <= zone.x_max and zone.y_min <= ny <= zone.y_max:
                area = (zone.x_max - zone.x_min) * (zone.y_max - zone.y_min)
                if area < best_area:
                    best = zone
                    best_area = area
        return best

    def is_entry_zone(self, camera_id: str, cy: int) -> bool:
        if camera_id != ENTRY_CAMERA:
            return False
        return (cy / self.frame_height) > (ENTRY_LINE_Y_NORMALIZED * 0.7)

    def is_staff_zone(self, camera_id: str) -> bool:
        return STAFF_CAMERA is not None and camera_id == STAFF_CAMERA

    def is_billing_zone(self, zone: Optional[Zone]) -> bool:
        if zone is None:
            return False
        return "BILLING" in zone.zone_id.upper() or "BILLING" in zone.sku_zone.upper()

    def get_entry_line_y(self) -> int:
        return int(self.frame_height * ENTRY_LINE_Y_NORMALIZED)

    def to_store_layout_json(self) -> dict:
        zones_out = []
        for cam_id, zones in CAMERA_ZONE_MAP.items():
            for z in zones:
                zones_out.append({
                    "zone_id":  z.zone_id,
                    "name":     z.name,
                    "sku_zone": z.sku_zone,
                    "camera_id": cam_id,
                    "bounds_normalized": {
                        "x_min": z.x_min, "y_min": z.y_min,
                        "x_max": z.x_max, "y_max": z.y_max,
                    },
                })
        return {
            "store_id":   STORE_ID,
            "store_name": STORE_NAME,
            "open_hours": {"start": "10:00", "end": "22:00"},
            "cameras":    list(CAMERA_ZONE_MAP.keys()),
            "zones":      zones_out,
        }
