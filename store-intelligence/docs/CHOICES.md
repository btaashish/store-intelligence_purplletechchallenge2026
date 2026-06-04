# CHOICES.md — Engineering Decision Record

Three significant architectural decisions made during this challenge, with full reasoning.

---

## Decision 1: Detection Model — HOG vs. YOLOv8 vs. VLM

### The Decision
Primary person detector: **OpenCV HOG** with Kalman filter tracking.

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **YOLOv8n** | High accuracy, handles occlusion well, fast GPU inference | Requires model download (~6MB weights); sandbox env blocks GitHub releases |
| **HOG (chosen)** | Zero-dependency, works offline, well-understood failure modes, fast on CPU | Less robust on partial occlusion, struggles with very small detections |
| **FastRCNN MobileNet (torchvision)** | Available in torchvision, decent accuracy | Also requires model download (403 Forbidden in sandbox) |
| **VLM (Claude Vision / GPT-4V)** | Could do staff detection, zone classification, scene understanding in one call | API latency makes per-frame infeasible; cost at 30fps × 20min × 5 cams = 180,000 frames |
| **Background Subtraction only** | Fastest, works at any resolution | High false positive rate (displays, lighting changes), can't distinguish people from objects |

### What AI Suggested
Claude recommended YOLOv8s as the starting point, noting that it's purpose-built for exactly this use case (COCO-pretrained on person detection, ByteTrack integration via supervision library). It also suggested using a VLM for the staff detection sub-problem specifically.

### What I Chose and Why
HOG for the core detection. This was a pragmatic decision forced by the build environment constraint — model weights couldn't be downloaded. HOG is a well-understood computer vision technique that works reliably on 1080p footage where subjects are 2–6 meters from the camera.

For the VLM suggestion on staff detection: I evaluated this and chose not to use it for per-frame analysis for cost/latency reasons. Instead I implemented a two-layer approach: camera-level staff identification (CAM_4 is backroom-only, all detections are staff) and session-level heuristics (tracks exclusively in billing without purchase correlation).

**Production recommendation**: YOLOv8s + ByteTrack + OSNet Re-ID. This is the industry standard for retail person counting and would address the partial occlusion edge cases much better than HOG. The pipeline code is structured to make swapping the detector a single-function change in `tracker.py`.

---

## Decision 2: Event Schema Design

### The Decision
The event schema includes `session_seq`, `confidence` (never suppressed), and separate `BILLING_QUEUE_JOIN` / `BILLING_QUEUE_ABANDON` event types rather than enriching `ZONE_ENTER` / `ZONE_EXIT`.

### Options Considered

**On `session_seq`:**
- Option A: Include as metadata field (chosen)
- Option B: Omit — can be reconstructed from timestamps
- Option C: Include as top-level field

I included it as metadata because it dramatically simplifies debugging conversion funnel issues. When a visitor's ENTRY→BILLING path has a gap, session_seq lets you find exactly which event is missing. This was a direct lesson from retail analytics tooling — reconstruction from timestamps is fragile when events arrive out of order.

**On confidence thresholds:**
- Option A: Filter out events below a confidence threshold (e.g., drop all < 0.3)
- Option B: Emit all detections with their actual confidence (chosen)

The spec explicitly says "do not suppress low-confidence events." I agree with this philosophy: downstream systems can apply their own thresholds. Suppressing at detection time discards information that may be valuable for calibration, audit, and debugging. A low-confidence ENTRY event may still be correct — removing it would undercount visitors.

**On billing event types:**
- Option A: Reuse `ZONE_ENTER` / `ZONE_EXIT` with metadata flags
- Option B: Dedicated event types for billing queue states (chosen)

Separate event types make API queries simpler (filter `event_type = BILLING_QUEUE_ABANDON` vs. parsing metadata), and they're semantically clearer in the event log. The billing queue is a different kind of state than zone dwell — it has direct business impact (queue abandonment = lost conversion).

### What AI Suggested
Claude suggested making `visitor_id` a full UUID rather than the short `VIS_xxxxxx` format. I overrode this: the short format is more readable in logs, easier to say in a post-submission video ("visitor VIS_c8a2f1"), and the 6-hex space (16M values) is more than sufficient for a single store session. Collision probability at 1000 visitors/day is negligible.

---

## Decision 3: API Architecture — Compute-on-Read vs. Pre-aggregated Cache

### The Decision
**Compute-on-read**: all metrics endpoints (`/metrics`, `/funnel`, `/heatmap`) recompute directly from the events table on every API call.

### Options Considered

**Option A: Compute-on-read (chosen)**
- Metrics always fresh
- No cache invalidation complexity
- Simple codebase — one code path
- Slightly higher per-request latency (~50–200ms depending on event count)

**Option B: Pre-aggregated materialized views (Redis + background worker)**
- Sub-millisecond response times
- Complex: cache invalidation on ingest, cache warming on startup, handling stale data
- Required for 40 stores × high event throughput

**Option C: Hybrid — cache hourly aggregates, compute recent window live**
- Best of both worlds for production
- Highest implementation complexity for this submission

### What AI Suggested
Claude's first suggestion was Option B (Redis cache with a background worker updating metrics every 30 seconds). This is absolutely the right call for production at scale. I agreed with the architecture for production but overrode it for this submission for two reasons:

1. **The challenge is 48 hours**: Adding Redis + a background worker + cache invalidation logic doubles the surface area for bugs during the evaluation period.
2. **Correctness > speed**: The evaluation spec says "Real-time — not cached from yesterday" — this primarily means the metrics should reflect today's events, not that response latency must be sub-millisecond. Compute-on-read achieves this while being provably correct.

**Production migration path**: Replace `compute_store_metrics()` with a cached version that reads from a Redis hash updated by a Celery task every 30 seconds. The API contract doesn't change — just the implementation behind it.

### Database Choice: SQLite vs. PostgreSQL

Same decision node: chose SQLite with WAL (Write-Ahead Log) mode.

SQLite WAL allows concurrent reads during writes — critical because the pipeline is writing events while the API is reading them. At our event volume (up to ~500 events/batch, store is open ~12 hours/day), SQLite handles this comfortably. The indexed `(store_id, timestamp)` query path is O(log n) regardless of engine.

If this challenge required 40 stores ingesting concurrently, I would switch to PostgreSQL — the connection pooling and multi-writer support would be necessary. For a single-store submission, the operational simplicity of SQLite wins.
