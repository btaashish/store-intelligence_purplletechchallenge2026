# DESIGN.md — Store Intelligence System Architecture

## System Overview

This system converts raw CCTV footage from a Purplle retail store (Mumbai Store 1076 (ST1008)) into real-time business analytics. The pipeline transforms pixels into the store's North Star metric: **offline conversion rate**.

```
CCTV Clips (mp4)
     │
     ▼
┌─────────────────────────────────────┐
│         Detection Layer             │
│  HOG Person Detector (OpenCV)       │
│  + Kalman Filter Tracker            │
│  + Colour Histogram Re-ID           │
│  + Zone Assigner                    │
│  + Entry/Exit Direction Detector    │
└──────────────┬──────────────────────┘
               │  Structured JSONL events
               ▼
┌─────────────────────────────────────┐
│         Event Ingest API            │
│  POST /events/ingest                │
│  Idempotent by event_id             │
│  SQLite (WAL mode) persistence      │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│       Intelligence API (FastAPI)    │
│  GET /stores/{id}/metrics           │
│  GET /stores/{id}/funnel            │
│  GET /stores/{id}/heatmap           │
│  GET /stores/{id}/anomalies         │
│  GET /health                        │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│         Live Dashboard              │
│  Auto-refreshing web UI (10s)       │
│  Metrics + Funnel + Heatmap +       │
│  Anomaly cards                      │
└─────────────────────────────────────┘
```

---

## Stage 1: Detection Layer

### Camera Role Mapping

After visual inspection of the 5 uploaded clips:

| File  | Camera ID       | Role          | Key Features Visible |
|-------|-----------------|---------------|----------------------|
| CAM_1 | CAM2    | Main floor    | Left Shelf (Z01) |
| CAM_2 | CAM3    | Main floor    | Centre Shelf (Z02) |
| CAM_3 | cam1    | Entry/Exit    | Entry/Exit threshold |
| CAM_4 | CAM4     | Backroom      | Right Shelf (Z03) |
| CAM_5 | PURPLLE_MUM_1076_CAM6  | Billing       | Billing Counter Queue |

### Person Detection: HOG + Background Subtraction

**Choice**: OpenCV HOG (Histogram of Oriented Gradients) person detector with NMS post-processing.

**Why not a DL model?** I evaluated YOLOv8n as the primary option — it would give better accuracy, especially for partial occlusion. However, the sandbox environment blocks external model downloads (GitHub releases 403 Forbidden). HOG gives reliable results on 1080p retail footage where people are at mid-range distances (2–6m from camera), which is exactly the case here.

**HOG parameters tuned for retail footage:**
- `winStride=(8,8)` — dense sliding window for close-range detection
- `scale=1.05` — fine scale pyramid to catch multiple person sizes  
- `HOG_SCORE_THRESH=0.3` — permissive to avoid false negatives; low-conf events emitted with actual score
- Frame processed at 960×540 (half-res) for speed; detections scaled back to 1920×1080

### Tracking: Kalman Filter + IoU Matching

A lightweight SORT-inspired tracker:
1. Each track maintains a Kalman filter over `[cx, cy, w, h, vx, vy, vw, vh]`
2. Hungarian-like IoU matching greedy assignment (sufficient for low-density retail)
3. Tracks confirmed after `min_hits=2` detections
4. Tracks retired after `max_age=45` frames (~1.5s) without update

### Re-ID: Colour Histogram Similarity

When a detection doesn't match any active track, the pipeline checks recently-lost tracks using HSV colour histogram similarity (Bhattacharyya distance). If similarity < 0.35, the same visitor_id is reassigned and a `REENTRY` event is emitted.

This approach works well for: re-entry within ~2 minutes (before the lost track is purged). It degrades gracefully for longer gaps. A production system would use OSNet/torchreid — documented in CHOICES.md.

### Staff Detection

Two-layer approach:
1. **Camera-level**: No backroom camera in ST1008 — all persons flagged `is_staff=True` with `confidence=1.0`
2. **Session-level**: Any track that appears exclusively in BILLING for >80% of its life without corresponding transaction activity is flagged as likely staff (cashier)

---

## Stage 2: Event Schema

Every event is emitted as JSON (JSONL) conforming to the required schema. Key design decisions:

- `event_id`: UUID v4, globally unique, used for idempotent ingest
- `visitor_id`: Deterministic 6-hex token derived from track_id (`VIS_xxxxxx`)
- `timestamp`: Derived from `clip_start_ts + (frame_idx / fps)` — accurate to the second
- `confidence`: Never suppressed, even at 0.01 — low-conf events are flagged, not dropped
- `session_seq`: Monotonically increasing ordinal per visitor session — useful for funnel debugging

---

## Stage 3: Intelligence API

### Framework: FastAPI + SQLite (WAL mode)

FastAPI chosen for: async-native, Pydantic validation baked in, auto-generated OpenAPI docs, production-ready with uvicorn.

SQLite chosen for: zero-infrastructure, WAL mode provides non-blocking reads, handles our event volume (~5,000–50,000 events per store per day) easily. The index on `(store_id, timestamp)` makes all range queries O(log n).

### Metric Computation Philosophy

All metrics computed directly from the events table on each API call — no pre-aggregated cache. This means:
- Always fresh (no stale cache invalidation problem)
- Slightly slower for large datasets (acceptable for our scale)
- Easy to reason about

The `build_sessions()` function is the core abstraction: it groups events by `visitor_id` and builds a session object with entry time, exit time, zones visited, billing status, and dwell time. All metrics derive from this.

### Conversion Rate Calculation

```
conversion_rate = visitors_with_purchase / total_unique_visitors
```

Visitor "purchased" = they were in BILLING zone within 5 minutes before a POS transaction timestamp. This time-window correlation is the standard approach when no customer_id exists in POS data (which is the case here — Purplle's actual ST1008 transaction data has no customer identifier).

---

## AI-Assisted Decisions

### 1. Re-ID Strategy: Histogram vs. Deep Features

I asked Claude to evaluate three Re-ID approaches for this use case:
1. **Colour histogram** (fast, no pretrained weights, works offline)
2. **OSNet / torchreid** (deep appearance features, high accuracy, requires model download)
3. **Bounding box trajectory matching** (position-based, fast, brittle on crowd)

Claude suggested OSNet as the best choice for production. I agreed with the principle but chose colour histogram for the submission because model downloads were blocked in the build environment. I documented this trade-off in CHOICES.md. If this were a production deployment, OSNet would be correct.

### 2. Storage Engine: SQLite vs. PostgreSQL vs. Redis

Claude initially suggested a Redis + PostgreSQL stack (Redis for real-time queue depth, Postgres for events). This is the right architecture for 40 stores sending 100 events/sec each. For this challenge with one store and batch ingestion, that would be significant over-engineering. I overrode this suggestion and chose SQLite with WAL mode, which achieves the same functional goals with zero infrastructure cost. I'd switch to PostgreSQL before production at scale.

### 3. Anomaly Detection: ML vs. Rule-Based

Claude suggested using an isolation forest or autoencoder for anomaly detection, trained on historical event patterns. I disagreed for this submission: (a) we have no training data, (b) the challenge requires specific anomaly types (`BILLING_QUEUE_SPIKE`, `CONVERSION_DROP`), (c) rule-based thresholds are explainable and debuggable in a retail context. The suggested_action strings are more useful to a store manager than an anomaly score. I would revisit ML anomaly detection once we have 30+ days of multi-store event history.
