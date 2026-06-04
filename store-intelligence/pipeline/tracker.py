"""
tracker.py — Person detection and multi-object tracking with Re-ID.

Uses OpenCV HOG + Kalman filter tracking + colour histogram Re-ID.
"""

import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

DETECT_RESIZE_W  = 960
DETECT_RESIZE_H  = 540
HOG_WIN_STRIDE   = (8, 8)
HOG_PADDING      = (8, 8)
HOG_SCALE        = 1.05
HOG_SCORE_THRESH = 0.28       # permissive to catch partial detections
NMS_OVERLAP      = 0.40
TRACK_MAX_AGE    = 30          # frames before track retired
TRACK_MIN_HITS   = 1           # confirm on first detection (retail = sparse crowd)
TRACK_IOU_THRESH = 0.25
REID_HIST_THRESH = 0.32
STAFF_ONLY_CAMS  = set()   # No dedicated backroom camera in ST1008 / Mumbai 1076


@dataclass
class KalmanTrack:
    track_id: int
    bbox: np.ndarray
    kalman: cv2.KalmanFilter
    hits: int = 1
    age: int = 0
    time_since_update: int = 0
    colour_hist: Optional[np.ndarray] = None
    is_confirmed: bool = False
    is_staff: bool = False
    appearance_vecs: List[np.ndarray] = field(default_factory=list)


def _build_kalman() -> cv2.KalmanFilter:
    kf = cv2.KalmanFilter(8, 4)
    kf.measurementMatrix = np.eye(4, 8, dtype=np.float32)
    kf.transitionMatrix  = np.eye(8, dtype=np.float32)
    for i in range(4):
        kf.transitionMatrix[i, i+4] = 1.0
    kf.processNoiseCov   = np.eye(8, dtype=np.float32) * 0.03
    kf.measurementNoiseCov = np.eye(4, dtype=np.float32) * 0.5
    kf.errorCovPost      = np.eye(8, dtype=np.float32)
    return kf


def _bbox_to_meas(bbox) -> np.ndarray:
    x, y, w, h = [float(v) for v in bbox]
    return np.array([[x+w/2],[y+h/2],[w],[h]], dtype=np.float32)


def _state_to_bbox(state: np.ndarray) -> np.ndarray:
    flat = state.flatten()
    cx, cy, w, h = float(flat[0]), float(flat[1]), float(flat[2]), float(flat[3])
    return np.array([cx-w/2, cy-h/2, w, h], dtype=np.float32)


def _iou(b1, b2) -> float:
    b1 = [float(v) for v in np.array(b1).flatten()[:4]]
    b2 = [float(v) for v in np.array(b2).flatten()[:4]]
    x1 = max(b1[0], b2[0]); y1 = max(b1[1], b2[1])
    x2 = min(b1[0]+b1[2], b2[0]+b2[2]); y2 = min(b1[1]+b1[3], b2[1]+b2[3])
    inter = max(0.0, x2-x1) * max(0.0, y2-y1)
    union = b1[2]*b1[3] + b2[2]*b2[3] - inter
    return inter / (union + 1e-6)


def _colour_hist(frame, bbox, bins=32) -> np.ndarray:
    x, y, w, h = [int(float(v)) for v in np.array(bbox).flatten()[:4]]
    x, y = max(0, x), max(0, y)
    x2, y2 = min(frame.shape[1], x+w), min(frame.shape[0], y+h)
    if x2 <= x or y2 <= y:
        return np.zeros(bins*3, dtype=np.float32)
    roi = frame[y:y2, x:x2]
    roi = roi[:max(1, (y2-y)//2), :]   # upper body only
    if roi.size == 0:
        return np.zeros(bins*3, dtype=np.float32)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_h = cv2.calcHist([hsv],[0],None,[bins],[0,180]).flatten()
    s_h = cv2.calcHist([hsv],[1],None,[bins],[0,256]).flatten()
    v_h = cv2.calcHist([hsv],[2],None,[bins],[0,256]).flatten()
    hist = np.concatenate([h_h, s_h, v_h]).astype(np.float32)
    norm = np.linalg.norm(hist)
    return hist / (norm + 1e-6)


def _hist_sim(h1, h2) -> float:
    return float(cv2.compareHist(
        h1.astype(np.float32), h2.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA))


class PersonTracker:
    def __init__(self, camera_id: str, scale_w: float = 1.0, scale_h: float = 1.0):
        self.camera_id = camera_id
        self.scale_w = scale_w
        self.scale_h = scale_h
        self._next_id = 1
        self._tracks: Dict[int, KalmanTrack] = {}
        self._lost: List[KalmanTrack] = []
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._frame_count = 0

    def detect(self, frame) -> Tuple[List[np.ndarray], List[float]]:
        small = cv2.resize(frame, (DETECT_RESIZE_W, DETECT_RESIZE_H))
        rects, weights = self._hog.detectMultiScale(
            small, winStride=HOG_WIN_STRIDE, padding=HOG_PADDING, scale=HOG_SCALE)
        if len(rects) == 0:
            return [], []
        weights = weights.flatten()
        mask = weights >= HOG_SCORE_THRESH
        rects, weights = rects[mask], weights[mask]
        if len(rects) == 0:
            return [], []
        try:
            indices = cv2.dnn.NMSBoxes(
                [(int(r[0]),int(r[1]),int(r[2]),int(r[3])) for r in rects],
                weights.tolist(), float(HOG_SCORE_THRESH), float(NMS_OVERLAP))
            if len(indices) == 0:
                return [], []
            indices = indices.flatten()
            rects, weights = rects[indices], weights[indices]
        except Exception:
            pass  # fallback: keep all detections
        scaled = []
        for r in rects:
            scaled.append(np.array([
                int(r[0]*self.scale_w), int(r[1]*self.scale_h),
                int(r[2]*self.scale_w), int(r[3]*self.scale_h)
            ], dtype=np.float32))
        return scaled, weights.tolist()

    def update(self, frame) -> Dict[int, KalmanTrack]:
        self._frame_count += 1
        for t in self._tracks.values():
            t.kalman.predict()
            t.age += 1
            t.time_since_update += 1

        detections, scores = self.detect(frame)
        track_ids = list(self._tracks.keys())
        unmatched = list(range(len(detections)))
        matched = []

        if track_ids and detections:
            cost = np.zeros((len(detections), len(track_ids)), dtype=np.float32)
            for di, det in enumerate(detections):
                for ti, tid in enumerate(track_ids):
                    pred = _state_to_bbox(self._tracks[tid].kalman.statePost[:4])
                    cost[di, ti] = _iou(det, pred)
            used = set()
            for di in range(len(detections)):
                best_ti = int(np.argmax(cost[di]))
                if cost[di, best_ti] >= TRACK_IOU_THRESH and best_ti not in used:
                    matched.append((di, track_ids[best_ti]))
                    used.add(best_ti)
                    if di in unmatched:
                        unmatched.remove(di)

        for di, tid in matched:
            t = self._tracks[tid]
            t.kalman.correct(_bbox_to_meas(detections[di]))
            t.bbox = detections[di]
            t.hits += 1
            t.time_since_update = 0
            t.colour_hist = _colour_hist(frame, detections[di])
            t.appearance_vecs = (t.appearance_vecs + [t.colour_hist])[-10:]
            if t.hits >= TRACK_MIN_HITS:
                t.is_confirmed = True
            if self.camera_id in STAFF_ONLY_CAMS:
                t.is_staff = True

        for di in unmatched:
            hist = _colour_hist(frame, detections[di])
            is_re, lost_t = self._check_reid(hist)
            if is_re and lost_t:
                nt = self._new_track(detections[di], hist, reuse_id=lost_t.track_id)
                nt.is_staff = lost_t.is_staff
                self._tracks[nt.track_id] = nt
                self._lost = [l for l in self._lost if l.track_id != lost_t.track_id]
            else:
                nt = self._new_track(detections[di], hist)
                if self.camera_id in STAFF_ONLY_CAMS:
                    nt.is_staff = True
                self._tracks[nt.track_id] = nt

        stale = [tid for tid, t in self._tracks.items()
                 if t.time_since_update > TRACK_MAX_AGE]
        for tid in stale:
            lost = self._tracks.pop(tid)
            if lost.is_confirmed:
                self._lost.append(lost)
                if len(self._lost) > 150:
                    self._lost.pop(0)

        return {tid: t for tid, t in self._tracks.items() if t.is_confirmed}

    def _check_reid(self, hist) -> Tuple[bool, Optional[KalmanTrack]]:
        best_sim, best = 9999.0, None
        for lt in self._lost:
            if lt.colour_hist is None:
                continue
            sim = _hist_sim(hist, lt.colour_hist)
            if sim < best_sim:
                best_sim = sim
                best = lt
        return (best_sim < REID_HIST_THRESH, best)

    def _new_track(self, bbox, hist, reuse_id=None) -> KalmanTrack:
        kf = _build_kalman()
        meas = _bbox_to_meas(bbox)
        kf.statePre  = np.zeros((8,1), dtype=np.float32)
        kf.statePost = np.zeros((8,1), dtype=np.float32)
        kf.statePre[:4]  = meas
        kf.statePost[:4] = meas
        kf.correct(meas)
        tid = reuse_id if reuse_id is not None else self._next_id
        if reuse_id is None:
            self._next_id += 1
        t = KalmanTrack(track_id=tid, bbox=bbox, kalman=kf,
                        colour_hist=hist, appearance_vecs=[hist])
        if TRACK_MIN_HITS <= 1:
            t.is_confirmed = True
        return t
