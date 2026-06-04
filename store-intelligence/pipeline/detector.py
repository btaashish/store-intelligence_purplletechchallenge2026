"""
Person detection using OpenCV Background Subtraction + HOG validation.
Robust for retail CCTV overhead/angled cameras without requiring external model weights.
"""

import cv2
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class Detection:
    bbox: Tuple[int, int, int, int]  # x, y, w, h (in original frame coords)
    confidence: float
    frame_idx: int
    timestamp_ms: float


class PersonDetector:
    """
    Multi-stage person detector:
    1. Background subtraction (MOG2) for motion detection
    2. Contour filtering for person-sized blobs
    3. HOG validation for high-confidence detections
    
    Designed for retail CCTV: overhead/angled angles, indoor lighting variation.
    """

    def __init__(
        self,
        min_person_area: int = 1200,
        max_person_area: int = 80000,
        min_aspect_ratio: float = 0.5,
        bg_history: int = 300,
        bg_threshold: float = 40.0,
        process_width: int = 640,
    ):
        self.min_person_area = min_person_area
        self.max_person_area = max_person_area
        self.min_aspect_ratio = min_aspect_ratio
        self.process_width = process_width

        # Background subtractor - long history to handle varying store lighting
        self.bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=bg_history, varThreshold=bg_threshold, detectShadows=False
        )

        # HOG person detector for validation
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

        # Morphological kernels
        self.kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

        self._frame_count = 0
        self._scale_x = 1.0
        self._scale_y = 1.0

    def _preprocess(self, frame: np.ndarray) -> Tuple[np.ndarray, float, float]:
        """Resize frame for processing, return scale factors."""
        h, w = frame.shape[:2]
        scale = self.process_width / w
        new_h = int(h * scale)
        small = cv2.resize(frame, (self.process_width, new_h))
        return small, scale, scale

    def _detect_bg_subtraction(
        self, small_frame: np.ndarray
    ) -> List[Tuple[int, int, int, int]]:
        """Background subtraction + contour filtering."""
        fg = self.bg_sub.apply(small_frame)

        # Morphological cleanup
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self.kernel_close)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel_open)

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        blobs = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self.min_person_area or area > self.max_person_area:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            # Person heuristic: not too wide relative to height
            aspect = h / max(w, 1)
            if aspect < self.min_aspect_ratio:
                continue
            blobs.append((x, y, w, h))

        return blobs

    def _merge_overlapping(
        self, boxes: List[Tuple[int, int, int, int]], overlap_threshold: float = 0.3
    ) -> List[Tuple[int, int, int, int]]:
        """Merge overlapping bounding boxes via NMS."""
        if not boxes:
            return []

        rects = np.array([[x, y, x + w, y + h] for x, y, w, h in boxes], dtype=float)
        # Simple greedy NMS
        keep = []
        used = [False] * len(rects)

        for i in range(len(rects)):
            if used[i]:
                continue
            keep.append(i)
            for j in range(i + 1, len(rects)):
                if used[j]:
                    continue
                # IoU
                ix1 = max(rects[i][0], rects[j][0])
                iy1 = max(rects[i][1], rects[j][1])
                ix2 = min(rects[i][2], rects[j][2])
                iy2 = min(rects[i][3], rects[j][3])
                if ix2 <= ix1 or iy2 <= iy1:
                    continue
                inter = (ix2 - ix1) * (iy2 - iy1)
                a1 = (rects[i][2] - rects[i][0]) * (rects[i][3] - rects[i][1])
                a2 = (rects[j][2] - rects[j][0]) * (rects[j][3] - rects[j][1])
                union = a1 + a2 - inter
                if inter / union > overlap_threshold:
                    used[j] = True

        result = []
        for i in keep:
            x1, y1, x2, y2 = rects[i]
            result.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
        return result

    def detect(
        self, frame: np.ndarray, frame_idx: int, fps: float
    ) -> List[Detection]:
        """
        Detect people in frame.
        Returns list of Detection objects in original frame coordinates.
        """
        self._frame_count += 1
        small, sx, sy = self._preprocess(frame)

        blobs = self._detect_bg_subtraction(small)
        blobs = self._merge_overlapping(blobs)

        detections = []
        for x, y, w, h in blobs:
            # Scale back to original coordinates
            orig_x = int(x / sx)
            orig_y = int(y / sy)
            orig_w = int(w / sx)
            orig_h = int(h / sy)

            # Confidence based on area and aspect ratio
            area = w * h
            ideal_area = 4000  # typical person blob in 640-wide
            area_conf = min(area / ideal_area, 1.0) * 0.5 + 0.4
            confidence = min(area_conf, 0.95)

            detections.append(
                Detection(
                    bbox=(orig_x, orig_y, orig_w, orig_h),
                    confidence=confidence,
                    frame_idx=frame_idx,
                    timestamp_ms=frame_idx / fps * 1000,
                )
            )

        return detections

    def warmup(self, cap: cv2.VideoCapture, warmup_frames: int = 60):
        """Warm up background model using first N frames (no detections emitted)."""
        logger.info(f"Warming up background model with {warmup_frames} frames...")
        orig_pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        for _ in range(warmup_frames):
            ret, frame = cap.read()
            if not ret:
                break
            small, _, _ = self._preprocess(frame)
            self.bg_sub.apply(small)

        cap.set(cv2.CAP_PROP_POS_FRAMES, orig_pos)
        logger.info("Background model warmup complete")
