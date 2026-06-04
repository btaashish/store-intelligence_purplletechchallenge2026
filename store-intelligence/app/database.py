"""
database.py — SQLite persistence layer with async support via aiosqlite.

Schema:
    events        — all ingested store events
    sessions      — aggregated visitor sessions (computed on ingest)
    transactions  — POS transaction records

Design decisions:
    - SQLite chosen for: zero infra, works in docker, handles our event volume
    - aiosqlite wraps sqlite3 with asyncio for non-blocking FastAPI handlers
    - event_id is the primary key → natural idempotency for POST /events/ingest
    - Indices on (store_id, timestamp) for range-query performance
"""

import aiosqlite
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/store_intelligence.db")


# ─── Schema ───────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    visitor_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    zone_id         TEXT,
    dwell_ms        INTEGER NOT NULL DEFAULT 0,
    is_staff        INTEGER NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL DEFAULT 0.5,
    queue_depth     INTEGER,
    sku_zone        TEXT,
    session_seq     INTEGER,
    inserted_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_events_store_ts
    ON events (store_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_visitor
    ON events (visitor_id);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (event_type);

CREATE INDEX IF NOT EXISTS idx_events_store_type
    ON events (store_id, event_type, timestamp);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    basket_value    REAL NOT NULL,
    visitor_id      TEXT,           -- correlated after matching
    inserted_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_txn_store_ts
    ON transactions (store_id, timestamp);

CREATE TABLE IF NOT EXISTS anomaly_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_id      TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    anomaly_type    TEXT NOT NULL,
    severity        TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    description     TEXT,
    metric_value    REAL,
    resolved_at     TEXT
);
"""


# ─── Connection management ────────────────────────────────────────────────────

_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _db


async def init_db(db_path: str = DB_PATH):
    global _db
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA_SQL)
    await _db.commit()
    logger.info(f"Database initialised at {db_path}")


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ─── Event operations ─────────────────────────────────────────────────────────

async def insert_events_batch(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Insert a batch of events. Returns counts of accepted, rejected, duplicate.
    Idempotent: duplicate event_id is silently skipped.
    """
    db = await get_db()
    accepted = 0
    duplicate = 0
    rejected = 0

    sql = """
    INSERT OR IGNORE INTO events
        (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
         zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone, session_seq)
    VALUES
        (:event_id, :store_id, :camera_id, :visitor_id, :event_type, :timestamp,
         :zone_id, :dwell_ms, :is_staff, :confidence, :queue_depth, :sku_zone, :session_seq)
    """

    async with db.cursor() as cur:
        for evt in events:
            try:
                meta = evt.get("metadata", {}) or {}
                row = {
                    "event_id":   evt["event_id"],
                    "store_id":   evt["store_id"],
                    "camera_id":  evt["camera_id"],
                    "visitor_id": evt["visitor_id"],
                    "event_type": evt["event_type"],
                    "timestamp":  evt["timestamp"],
                    "zone_id":    evt.get("zone_id"),
                    "dwell_ms":   evt.get("dwell_ms", 0),
                    "is_staff":   1 if evt.get("is_staff") else 0,
                    "confidence": evt.get("confidence", 0.5),
                    "queue_depth": meta.get("queue_depth"),
                    "sku_zone":   meta.get("sku_zone"),
                    "session_seq": meta.get("session_seq"),
                }
                await cur.execute(sql, row)
                if cur.rowcount > 0:
                    accepted += 1
                else:
                    duplicate += 1
            except Exception as e:
                logger.warning(f"Failed to insert event {evt.get('event_id')}: {e}")
                rejected += 1

    await db.commit()
    return {"accepted": accepted, "duplicate": duplicate, "rejected": rejected}


async def get_events(
    store_id: str,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    event_types: Optional[List[str]] = None,
    exclude_staff: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch events for a store within a time window."""
    db = await get_db()
    conditions = ["store_id = ?"]
    params: List[Any] = [store_id]

    if exclude_staff:
        conditions.append("is_staff = 0")
    if start_ts:
        conditions.append("timestamp >= ?")
        params.append(start_ts)
    if end_ts:
        conditions.append("timestamp <= ?")
        params.append(end_ts)
    if event_types:
        placeholders = ",".join(["?" for _ in event_types])
        conditions.append(f"event_type IN ({placeholders})")
        params.extend(event_types)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM events WHERE {where} ORDER BY timestamp ASC"

    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_total_events() -> int:
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM events") as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def get_last_event_time(store_id: str, camera_id: Optional[str] = None) -> Optional[str]:
    db = await get_db()
    if camera_id:
        sql = "SELECT MAX(timestamp) FROM events WHERE store_id=? AND camera_id=?"
        params = [store_id, camera_id]
    else:
        sql = "SELECT MAX(timestamp) FROM events WHERE store_id=?"
        params = [store_id]
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


# ─── Transaction operations ───────────────────────────────────────────────────

async def insert_transactions(transactions: List[Dict[str, Any]]):
    db = await get_db()
    sql = """
    INSERT OR IGNORE INTO transactions (transaction_id, store_id, timestamp, basket_value)
    VALUES (:transaction_id, :store_id, :timestamp, :basket_value)
    """
    async with db.cursor() as cur:
        for txn in transactions:
            await cur.execute(sql, txn)
    await db.commit()


async def get_transactions(store_id: str, start_ts: str, end_ts: str) -> List[Dict]:
    db = await get_db()
    sql = """
    SELECT * FROM transactions
    WHERE store_id=? AND timestamp >= ? AND timestamp <= ?
    ORDER BY timestamp ASC
    """
    async with db.execute(sql, [store_id, start_ts, end_ts]) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def get_db_sync():
    """Synchronous SQLite connection for startup-time use (CSV loading)."""
    import sqlite3
    return sqlite3.connect(DB_PATH)


def insert_transactions_sync(transactions: List[Dict[str, Any]]) -> int:
    """
    Synchronous version of insert_transactions for use at startup before the
    async event loop is running. Idempotent via INSERT OR IGNORE.
    Returns number of rows inserted.
    """
    import sqlite3
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    sql = """
    INSERT OR IGNORE INTO transactions (transaction_id, store_id, timestamp, basket_value)
    VALUES (:transaction_id, :store_id, :timestamp, :basket_value)
    """
    inserted = 0
    try:
        cur = con.cursor()
        # Ensure table exists (may be called before async init)
        cur.executescript(SCHEMA_SQL)
        for txn in transactions:
            cur.execute(sql, txn)
            if cur.rowcount > 0:
                inserted += 1
        con.commit()
    finally:
        con.close()
    return inserted
