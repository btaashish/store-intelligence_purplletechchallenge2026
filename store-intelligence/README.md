# Store Intelligence System
**End-to-end CCTV → live analytics pipeline for Purplle's Mumbai store (ST1008 / Store 1076).**

An end-to-end retail analytics platform that converts raw CCTV footage into structured customer-behaviour events and real-time store intelligence.

The system processes entry, floor, and billing-area camera feeds, generates visitor activity events, correlates them with POS transactions, and exposes operational metrics through a FastAPI-based Intelligence API.

Key capabilities include:

Visitor entry and exit detection
Zone visit and dwell analytics
Billing queue monitoring
Re-entry detection
POS conversion tracking
Funnel analytics
Heatmap generation
Operational anomaly detection

## Quick Start

```bash
docker compose up
```

Then load POS data and events:

```bash
# Load POS transactions
cp /path/to/POS_sample_transactions.csv data/pos_transactions.csv

# Adapt and ingest external events
python pipeline/adapt_external_events.py \
  --input data/sample_events.jsonl \
  --output data/adapted_events.jsonl

python pipeline/ingest_events.py \
  --events-file data/adapted_events.jsonl \
  --api-url http://localhost:8000

# Or run the full detection pipeline on raw video
./pipeline/run.sh data/videos http://localhost:8000
```

## Architecture

```
CAM footage → pipeline/detect.py → JSONL events → POST /events/ingest → SQLite
External JSONL → pipeline/adapt_external_events.py → (normalised) → POST /events/ingest
POS CSV → loaded automatically on startup via POS_CSV_PATH env var → SQLite
SQLite → GET /metrics, /funnel, /heatmap, /anomalies
```

## Store Layout — ST1008 / Mumbai 1076

| File | Camera ID | Role | Zone |
|------|-----------|------|------|
| CAM_3_entry.mp4 / 1780501836199_entry_1.mp4 | cam1 | Entry/Exit threshold | ENTRY_ZONE |
| CAM_5_billing.mp4 / 1780501806240_billing_area.mp4 | PURPLLE_MUM_1076_CAM6 | Billing counter queue | PURPLLE_MUM_1076_Z_BILLING_01 |
| CAM_1_zone.mp4 / 1780501781702_zone.mp4 | CAM2 | Left Shelf | PURPLLE_MUM_1076_Z01 |
| CAM_2_zone.mp4 | CAM3 | Centre Shelf | PURPLLE_MUM_1076_Z02 |
| (4th zone cam) | CAM4 | Right Shelf | PURPLLE_MUM_1076_Z03 |

## Zone → Brand Mapping

| Zone | Brands |
|------|--------|
| PURPLLE_MUM_1076_Z01 (Left Shelf) | Faces Canada, Renee, Swiss Beauty, Maybelline, Alps Goodness, NY Bae |
| PURPLLE_MUM_1076_Z02 (Centre Shelf) | Good Vibes, DERMDOC, Foxtale, Juicy Chemistry, Beauty of Joseon, COSRX, Round Lab |
| PURPLLE_MUM_1076_Z03 (Right Shelf) | Lakme, Garnier, Neutrogena, Lotus Herbals, Carmesi, Bare Anatomy, GUBB, CUFFS N LASHES |

## API Endpoints

```
GET  /health
GET  /stores/{store_id}/metrics
GET  /stores/{store_id}/funnel
GET  /stores/{store_id}/heatmap
GET  /stores/{store_id}/anomalies
POST /events/ingest
```

Default store ID: `ST1008`

## Running Detection on a Single Camera

```bash
python pipeline/detect.py \
  --video data/videos/1780501836199_entry_1.mp4 \
  --camera-id cam1 \
  --output data/events/entry_events.jsonl \
  --store-id ST1008
```

## Adapting External JSONL Events

The external Purplle event format differs from the internal schema. Use the adapter:

```bash
python pipeline/adapt_external_events.py \
  --input sample_events.jsonl \
  --output adapted_events.jsonl
```

The adapter handles:
- Lowercase event types (`entry` → `ENTRY`, `zone_entered` → `ZONE_ENTER`, etc.)
- Different field names (`id_token`/`track_id` → `visitor_id`, `event_timestamp`/`event_time` → `timestamp`)
- Store code normalisation (`store_1076`/`ST1076` → `ST1008`)
- Queue events (`queue_completed` → `ZONE_EXIT`, `queue_abandoned` → `BILLING_QUEUE_ABANDON`)
- Microsecond timestamps → second-precision UTC

## Config

Store layout and camera/zone definitions: `config/store_layout.json`
