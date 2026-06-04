#!/usr/bin/env bash
# run.sh — Process all CCTV clips and feed events into the API
#
# Usage:
#   ./pipeline/run.sh [VIDEO_DIR] [API_URL]
#
# Example:
#   ./pipeline/run.sh /data/videos http://localhost:8000
#
# Updated for Store ST1008 / Mumbai 1076
# Camera filenames map to: cam1 (entry), CAM2-4 (shelf zones), PURPLLE_MUM_1076_CAM6 (billing)

set -euo pipefail

VIDEO_DIR="${1:-/data/videos}"
API_URL="${2:-http://localhost:8000}"
OUTPUT_DIR="${3:-/tmp/events}"
STORE_ID="${4:-ST1008}"

mkdir -p "$OUTPUT_DIR"

echo "========================================"
echo "  Purplle Store Intelligence Pipeline  "
echo "========================================"
echo "Video dir : $VIDEO_DIR"
echo "API URL   : $API_URL"
echo "Output    : $OUTPUT_DIR"
echo "Store ID  : $STORE_ID"
echo ""

# Camera assignment — filename pattern → camera_id
declare -A CAM_MAP
CAM_MAP["CAM_1_zone.mp4"]="CAM2"
CAM_MAP["CAM_2_zone.mp4"]="CAM3"
CAM_MAP["CAM_3_entry.mp4"]="cam1"
CAM_MAP["CAM_5_billing.mp4"]="PURPLLE_MUM_1076_CAM6"
# Timestamped variants (new naming convention)
CAM_MAP["1780501781702_zone.mp4"]="CAM2"
CAM_MAP["1780501806240_billing_area.mp4"]="PURPLLE_MUM_1076_CAM6"
CAM_MAP["1780501836199_entry_1.mp4"]="cam1"
CAM_MAP["1780501867720_entry_2.mp4"]="cam1"

# Priority order: entry first, then billing, then floor cams
PROCESS_ORDER=(
    "CAM_3_entry.mp4"
    "1780501836199_entry_1.mp4"
    "1780501867720_entry_2.mp4"
    "CAM_5_billing.mp4"
    "1780501806240_billing_area.mp4"
    "CAM_1_zone.mp4"
    "1780501781702_zone.mp4"
    "CAM_2_zone.mp4"
)

TOTAL_EVENTS=0

for FILENAME in "${PROCESS_ORDER[@]}"; do
    VIDEO_PATH="$VIDEO_DIR/$FILENAME"
    if [ ! -f "$VIDEO_PATH" ]; then
        echo "⚠️  Not found: $VIDEO_PATH — skipping"
        continue
    fi

    CAMERA_KEY="${CAM_MAP[$FILENAME]}"
    SAFE_KEY="${CAMERA_KEY//\//_}"
    OUTPUT_FILE="$OUTPUT_DIR/${SAFE_KEY}_events.jsonl"

    echo "── Processing $FILENAME ($CAMERA_KEY) ──"
    python3 "$(dirname "$0")/detect.py" \
        --video "$VIDEO_PATH" \
        --camera-id "$CAMERA_KEY" \
        --output "$OUTPUT_FILE" \
        --store-id "$STORE_ID"

    N=$(wc -l < "$OUTPUT_FILE" 2>/dev/null || echo 0)
    echo "   ✓ $N events → $OUTPUT_FILE"
    TOTAL_EVENTS=$((TOTAL_EVENTS + N))

    echo "   ↑ Ingesting into $API_URL/events/ingest ..."
    python3 "$(dirname "$0")/ingest_events.py" \
        --events-file "$OUTPUT_FILE" \
        --api-url "$API_URL" \
        --batch-size 500 || echo "   ⚠️  Ingest failed (API may not be running)"

    echo ""
done

echo "========================================"
echo "  Total events emitted: $TOTAL_EVENTS"
echo "  Check the dashboard: $API_URL/dashboard"
echo "========================================"
