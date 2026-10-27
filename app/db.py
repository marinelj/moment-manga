"""Tiny SQLite order store. One row per order; good enough for one server."""
import json
import sqlite3
import threading
import time
import uuid

from .settings import DATA_DIR

_lock = threading.Lock()
_conn = sqlite3.connect(DATA_DIR / "orders.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("""
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  created REAL, theme TEXT, style TEXT, video TEXT,
  status TEXT,            -- unpaid | paid | processing | done | failed
  progress INTEGER DEFAULT 0,
  provider TEXT, provider_ref TEXT, pay_url TEXT,
  result TEXT, error TEXT
)""")
_conn.commit()


def create(theme: str, style: str, video: str) -> dict:
    oid = uuid.uuid4().hex     # unguessable; also acts as the download capability
    with _lock:
        _conn.execute("INSERT INTO orders (id, created, theme, style, video, status) VALUES (?,?,?,?,?, 'unpaid')",
                      (oid, time.time(), theme, style, video))
        _conn.commit()
    return get(oid)


def get(oid: str) -> dict | None:
    row = _conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return dict(row) if row else None


def update(oid: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), oid))
        _conn.commit()


def mark_paid(oid: str) -> bool:
    """Idempotent: payment providers retry notifications."""
    with _lock:
        cur = _conn.execute("UPDATE orders SET status='paid' WHERE id=? AND status='unpaid'", (oid,))
        _conn.commit()
    return cur.rowcount == 1


def claim_next() -> dict | None:
    with _lock:
        row = _conn.execute("SELECT id FROM orders WHERE status='paid' ORDER BY created LIMIT 1").fetchone()
        if not row:
            return None
        _conn.execute("UPDATE orders SET status='processing', progress=1 WHERE id=?", (row["id"],))
        _conn.commit()
    return get(row["id"])


def requeue_stuck() -> None:
    """On boot, jobs that were mid-flight when the server stopped go back in the queue."""
    with _lock:
        _conn.execute("UPDATE orders SET status='paid', progress=0 WHERE status='processing'")
        _conn.commit()
