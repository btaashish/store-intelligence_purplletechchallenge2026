"""
adapt_external_events.py — Normalises the external Purplle JSONL event format
into the internal StoreEvent schema expected by /events/ingest.

External format has two schemas mixed in one file:
  1. Entry/exit events  — fields: event_type, id_token, store_code, camera_id,
                          event_timestamp, is_staff, gender_pred, age_pred, ...
  2. Zone events        — fields: event_type, track_id, store_id, camera_id,
                          zone_id, zone_name, zone_type, event_time, ...
  3. Queue events       — fields: queue_event_id, event_type, track_id, store_id,
                          camera_id, zone_id, queue_join_ts, wait_seconds, abandoned, ...

Internal schema (StoreEventIn):
  event_id, store_id, camera_id, visitor_id, event_type (UPPER),
  timestamp (YYYY-MM-DDTHH:MM:SSZ), zone_id, dwell_ms, is_staff,
  confidence, metadata{queue_depth, sku_zone, session_seq}

Usage:
    python adapt_external_events.py --input sample_events.jsonl --output adapted_events.jsonl
"""

import json
import uuid
import argparse
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Event type mapping ────────────────────────────────────────────────────────
EVENT_TYPE_MAP = {
    "entry":           "ENTRY",
    "exit":            "EXIT",
    "zone_entered":    "ZONE_ENTER",
    "zone_exited":     "ZONE_EXIT",
    "queue_completed": "ZONE_EXIT",        # served at billing → treated as successful exit
    "queue_abandoned": "BILLING_QUEUE_ABANDON",
}

STORE_ID_NORM = {
    "store_1076": "ST1008",
    "ST1076":     "ST1008",
}


def _normalise_ts(raw: str) -> str:
    """Convert any ISO-8601 variant to YYYY-MM-DDTHH:MM:SSZ (UTC, no microseconds)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw[:26], fmt)  # trim trailing noise
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"Unparseable timestamp: {raw!r}")


def _visitor_id(raw: str | int) -> str:
    """Normalise id_token / track_id to a consistent VIS_ prefix."""
    s = str(raw)
    if s.startswith("ID_") or s.startswith("VIS_"):
        return s
    hex_suffix = hex(hash(s) & 0xFFFFFF)[2:].zfill(6)
    return f"VIS_{hex_suffix}"


def adapt(raw: dict) -> dict | None:
    """Return a normalised event dict or None if the event should be skipped."""
    et_raw = raw.get("event_type", "")
    et = EVENT_TYPE_MAP.get(et_raw)
    if et is None:
        logger.debug(f"Skipping unknown event_type: {et_raw!r}")
        return None

    # ── Store ID ──────────────────────────────────────────────────────────────
    raw_store = raw.get("store_id") or raw.get("store_code") or "ST1008"
    store_id = STORE_ID_NORM.get(raw_store, raw_store)

    # ── Camera ID ─────────────────────────────────────────────────────────────
    camera_id = raw.get("camera_id", "cam1")

    # ── Visitor ID ────────────────────────────────────────────────────────────
    visitor_id = _visitor_id(
        raw.get("id_token") or raw.get("track_id") or uuid.uuid4().hex[:8]
    )

    # ── Timestamp ─────────────────────────────────────────────────────────────
    ts_raw = (
        raw.get("event_timestamp")
        or raw.get("event_time")
        or raw.get("queue_join_ts")
    )
    if not ts_raw:
        logger.warning(f"No timestamp in event: {raw}")
        return None
    timestamp = _normalise_ts(ts_raw)

    # ── Zone ─────────────────────────────────────────────────────────────────
    zone_id = raw.get("zone_id")

    # ── Dwell ─────────────────────────────────────────────────────────────────
    dwell_ms = 0
    wait_sec = raw.get("wait_seconds")
    if wait_sec is not None:
        dwell_ms = int(float(wait_sec) * 1000)

    # ── Staff flag ────────────────────────────────────────────────────────────
    is_staff = bool(raw.get("is_staff", False))

    # ── Metadata ─────────────────────────────────────────────────────────────
    queue_pos = raw.get("queue_position_at_join")
    sku_zone = zone_id.lower() if zone_id else None
    metadata = {
        "queue_depth":  queue_pos,
        "sku_zone":     sku_zone,
        "session_seq":  0,
    }

    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": et,
        "timestamp":  timestamp,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": 0.85,
        "metadata":   metadata,
    }


def adapt_file(input_path: str, output_path: str) -> tuple[int, int]:
    adapted = skipped = 0
    with open(input_path) as fin, open(output_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(f"Malformed JSON: {exc}")
                skipped += 1
                continue
            result = adapt(raw)
            if result:
                fout.write(json.dumps(result) + "\n")
                adapted += 1
            else:
                skipped += 1
    return adapted, skipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Normalise external Purplle events to internal schema")
    parser.add_argument("--input",  required=True, help="Input JSONL path (external format)")
    parser.add_argument("--output", required=True, help="Output JSONL path (internal format)")
    args = parser.parse_args()

    adapted, skipped = adapt_file(args.input, args.output)
    logger.info(f"Done: {adapted} events adapted, {skipped} skipped → {args.output}")
