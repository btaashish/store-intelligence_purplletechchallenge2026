"""
generate_demo_events.py — Generates realistic synthetic events for Store ST1008 (Mumbai 1076).

Updated to match the new store layout: ST1008 / store_1076, cameras cam1/CAM2/CAM3/CAM4/PURPLLE_MUM_1076_CAM6,
and zone IDs from sample_events JSONL (PURPLLE_MUM_1076_Z01/Z02/Z03/Z_BILLING_01).
"""
import json, uuid, random, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from emit import make_visitor_id

STORE_ID = "ST1008"
BASE_TIME = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)

ZONES = [
    "PURPLLE_MUM_1076_Z01",
    "PURPLLE_MUM_1076_Z02",
    "PURPLLE_MUM_1076_Z03",
]

CAMERAS = {
    "ENTRY_ZONE":                    "cam1",
    "PURPLLE_MUM_1076_Z01":          "CAM2",
    "PURPLLE_MUM_1076_Z02":          "CAM3",
    "PURPLLE_MUM_1076_Z03":          "CAM4",
    "PURPLLE_MUM_1076_Z_BILLING_01": "PURPLLE_MUM_1076_CAM6",
}


def ts(minutes: float) -> str:
    t = BASE_TIME + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def evt(visitor_id, event_type, minutes, zone_id=None, dwell_ms=0,
        is_staff=False, confidence=0.82, queue_depth=None, sku_zone=None, seq=1):
    cam = CAMERAS.get(zone_id or "ENTRY_ZONE", "CAM2")
    if event_type in ("ENTRY", "EXIT", "REENTRY"):
        cam = "cam1"
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  cam,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  ts(minutes),
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": queue_depth, "sku_zone": sku_zone, "session_seq": seq},
    }


def simulate_visitor(vid, start_min, zones_to_visit, reaches_billing=False,
                     purchased=False, is_staff=False):
    events = []
    seq = 0
    t = start_min

    def e(et, **kw):
        nonlocal seq; seq += 1
        return evt(vid, et, t, seq=seq, is_staff=is_staff, **kw)

    events.append(e("ENTRY"))
    t += random.uniform(0.1, 0.5)

    for zone in zones_to_visit:
        events.append(e("ZONE_ENTER", zone_id=zone, sku_zone=zone.lower()))
        dwell = random.randint(20000, 180000)
        t += dwell / 60000
        if dwell >= 30000:
            events.append(e("ZONE_DWELL", zone_id=zone, dwell_ms=dwell, sku_zone=zone.lower()))
        events.append(e("ZONE_EXIT", zone_id=zone, dwell_ms=dwell, sku_zone=zone.lower()))

    if reaches_billing:
        billing_zone = "PURPLLE_MUM_1076_Z_BILLING_01"
        q_depth = random.randint(0, 4)
        if q_depth > 0:
            events.append(e("BILLING_QUEUE_JOIN", zone_id=billing_zone, queue_depth=q_depth))
        else:
            events.append(e("ZONE_ENTER", zone_id=billing_zone))
        t += random.uniform(1, 8)
        if not purchased:
            events.append(e("BILLING_QUEUE_ABANDON", zone_id=billing_zone,
                            dwell_ms=int(random.uniform(1, 8) * 60000)))
        else:
            events.append(e("ZONE_EXIT", zone_id=billing_zone,
                            dwell_ms=int(random.uniform(2, 10) * 60000)))

    events.append(e("EXIT"))
    return events, t


def main(output_path: str, n_visitors: int = 80):
    all_events = []

    # Staff sessions
    for i in range(3):
        vid = make_visitor_id(500 + i)
        t = random.uniform(0, 5)
        e1 = evt(vid, "ENTRY", t, is_staff=True, confidence=0.95, seq=1)
        e2 = evt(vid, "ZONE_ENTER", t + 0.1, zone_id="PURPLLE_MUM_1076_Z01",
                 is_staff=True, confidence=0.95, seq=2)
        e3 = evt(vid, "ZONE_EXIT", t + 120, zone_id="PURPLLE_MUM_1076_Z01",
                 dwell_ms=7200000, is_staff=True, confidence=0.95, seq=3)
        all_events.extend([e1, e2, e3])

    # Customer sessions
    random.seed(42)
    for i in range(n_visitors):
        vid = make_visitor_id(600 + i)
        start_min = random.uniform(0, 120)
        n_zones = random.randint(1, 3)
        zones = random.sample(ZONES, min(n_zones, len(ZONES)))
        reaches_billing = random.random() < 0.55
        purchased = reaches_billing and random.random() < 0.45
        events, _ = simulate_visitor(vid, start_min, zones,
                                     reaches_billing=reaches_billing,
                                     purchased=purchased)
        all_events.extend(events)

    # Re-entries: 5 returning visitors
    for i in range(5):
        vid = make_visitor_id(600 + i)
        start_min = random.uniform(60, 130)
        zones = random.sample(ZONES, 2)
        events, _ = simulate_visitor(vid, start_min, zones,
                                     reaches_billing=True, purchased=True)
        if events:
            events[0]["event_type"] = "REENTRY"
        all_events.extend(events)

    all_events.sort(key=lambda e: e["timestamp"])

    with open(output_path, "a") as f:
        for e in all_events:
            f.write(json.dumps(e) + "\n")

    print(f"Generated {len(all_events)} synthetic events → {output_path}")
    return all_events


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="data/events/synthetic_events.jsonl")
    p.add_argument("--visitors", type=int, default=80)
    args = p.parse_args()
    main(args.output, args.visitors)
