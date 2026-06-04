"""
ingest_events.py — Reads a JSONL events file and POSTs to /events/ingest.

Usage:
    python ingest_events.py --events-file events.jsonl --api-url http://localhost:8000
"""

import json
import argparse
import time
import logging
import sys
try:
    import httpx
except ImportError:
    import urllib.request as _urllib
    httpx = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def post_batch(api_url: str, events: list, retries: int = 3) -> bool:
    url = f"{api_url.rstrip('/')}/events/ingest"
    payload = json.dumps({"events": events}).encode()
    headers = {"Content-Type": "application/json"}

    for attempt in range(retries):
        try:
            if httpx:
                resp = httpx.post(url, content=payload, headers=headers, timeout=30.0)
                status = resp.status_code
                body = resp.text
            else:
                req = _urllib.Request(url, data=payload, headers=headers, method="POST")
                with _urllib.urlopen(req, timeout=30) as r:
                    status = r.status
                    body = r.read().decode()

            if status < 300:
                return True
            elif status < 500:
                logger.warning(f"API returned {status}: {body[:200]}")
                return False
            else:
                logger.warning(f"API 5xx {status}, retry {attempt+1}/{retries}")
                time.sleep(2 ** attempt)
        except Exception as e:
            logger.warning(f"Request failed: {e}, retry {attempt+1}/{retries}")
            time.sleep(2 ** attempt)

    return False


def ingest_file(events_file: str, api_url: str, batch_size: int = 500,
                realtime_delay: float = 0.0):
    events = []
    failed = 0
    total = 0
    batches_ok = 0
    batches_fail = 0

    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                events.append(event)
                total += 1
            except json.JSONDecodeError as e:
                logger.warning(f"Malformed event line: {e}")
                failed += 1
                continue

            if len(events) >= batch_size:
                ok = post_batch(api_url, events)
                if ok:
                    batches_ok += 1
                    logger.info(f"Ingested batch of {len(events)} events (total so far: {total})")
                else:
                    batches_fail += 1
                    logger.error(f"Failed to ingest batch of {len(events)} events")
                events = []

                if realtime_delay > 0:
                    time.sleep(realtime_delay * batch_size)

    # Last batch
    if events:
        ok = post_batch(api_url, events)
        if ok:
            batches_ok += 1
        else:
            batches_fail += 1

    logger.info(
        f"Done: {total} events, {batches_ok} batches OK, {batches_fail} batches failed, "
        f"{failed} malformed lines"
    )
    return batches_fail == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-file", required=True)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--realtime-delay", type=float, default=0.0,
                        help="Seconds of simulated delay per batch")
    args = parser.parse_args()

    ok = ingest_file(args.events_file, args.api_url, args.batch_size, args.realtime_delay)
    sys.exit(0 if ok else 1)
