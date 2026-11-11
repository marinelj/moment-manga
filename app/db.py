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
try:
    _conn.execute("ALTER TABLE orders ADD COLUMN lang TEXT DEFAULT 'en'")
except sqlite3.OperationalError:
    pass  # column already there
_conn.commit()
for column, kind in (
    ("product", "TEXT"), ("amount_yuan", "TEXT"), ("app_id", "TEXT"),
    ("seller_id", "TEXT"), ("buyer_token_hash", "TEXT"),
    ("alipay_trade_no", "TEXT"), ("paid_at", "REAL"), ("photo", "TEXT"),
    ("last_query_at", "REAL"), ("video_result", "TEXT"), ("generation", "TEXT"), ("api_style", "TEXT"),
    ("email", "TEXT"), ("notify_token", "TEXT"), ("done_at", "REAL"), ("notified_at", "REAL"),
    ("files_deleted_at", "REAL"), ("lead", "TEXT"),
):
    try:
        _conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {kind}")
    except sqlite3.OperationalError:
        pass
_conn.execute("""CREATE TABLE IF NOT EXISTS alipay_notify_events (
    notify_id TEXT PRIMARY KEY, out_trade_no TEXT NOT NULL,
    trade_no TEXT NOT NULL, event_type TEXT NOT NULL, received REAL NOT NULL
)""")
_conn.execute("""CREATE TABLE IF NOT EXISTS alipay_refunds (
    out_request_no TEXT PRIMARY KEY, out_trade_no TEXT NOT NULL,
    amount_yuan TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL
)""")
_conn.execute("""CREATE TABLE IF NOT EXISTS order_costs (
    order_id TEXT NOT NULL, step TEXT NOT NULL, model TEXT NOT NULL,
    units REAL NOT NULL, usd REAL NOT NULL, cached INTEGER NOT NULL, created REAL NOT NULL
)""")
_conn.commit()


_conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)")
_conn.commit()


def get_setting(key: str) -> str | None:
    with _lock:
        row = _conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key: str, value: str | None) -> None:
    with _lock:
        if value is None:
            _conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        else:
            _conn.execute("INSERT INTO app_settings VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                          (key, value, time.time()))
        _conn.commit()


def recent_orders(limit: int = 20) -> list[dict]:
    with _lock:
        rows = _conn.execute("""SELECT o.id, o.created, o.product, o.status, o.progress, o.generation, o.api_style, o.amount_yuan,
                o.error, COALESCE((SELECT SUM(usd) FROM order_costs c WHERE c.order_id=o.id), 0) AS cost_usd
            FROM orders o ORDER BY o.created DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in rows]


def add_cost(oid: str, step: str, model: str, units: float, usd: float, cached: bool = False) -> None:
    """Cost ledger: one row per paid model call (cached reuse is logged at $0)."""
    with _lock:
        _conn.execute("INSERT INTO order_costs VALUES (?,?,?,?,?,?,?)",
                      (oid, step, model, units, 0.0 if cached else usd, int(cached), time.time()))
        _conn.commit()


def order_costs(oid: str) -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM order_costs WHERE order_id=? ORDER BY created", (oid,)).fetchall()
    return [dict(row) for row in rows]


def order_cost_total(oid: str) -> float:
    with _lock:
        row = _conn.execute("SELECT COALESCE(SUM(usd), 0) FROM order_costs WHERE order_id=?", (oid,)).fetchone()
    return float(row[0])


def create(theme: str, style: str, video: str, lang: str = "en") -> dict:
    oid = uuid.uuid4().hex     # buyer access also requires the order cookie
    with _lock:
        _conn.execute("INSERT INTO orders (id, created, theme, style, video, lang, status) VALUES (?,?,?,?,?,?, 'unpaid')",
                      (oid, time.time(), theme, style, video, lang))
        _conn.commit()
    return get(oid)


def get(oid: str) -> dict | None:
    with _lock:
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


def record_alipay_notification(oid: str, notify_id: str, trade_no: str,
                               event_type: str, paid: bool) -> bool:
    """Commit the notification and paid transition together; duplicate events are safe."""
    with _lock:
        try:
            _conn.execute("INSERT INTO alipay_notify_events VALUES (?,?,?,?,?)",
                          (notify_id, oid, trade_no, event_type, time.time()))
        except sqlite3.IntegrityError:
            return False
        if paid:
            _conn.execute("""UPDATE orders SET status='paid', alipay_trade_no=?, paid_at=?
                WHERE id=? AND provider='alipay_web' AND status='unpaid'""",
                          (trade_no, time.time(), oid))
        _conn.commit()
        return True


def mark_alipay_paid_from_query(oid: str, trade_no: str) -> bool:
    with _lock:
        cur = _conn.execute("""UPDATE orders SET status='paid', alipay_trade_no=?, paid_at=?
            WHERE id=? AND provider='alipay_web' AND status='unpaid'""",
                            (trade_no, time.time(), oid))
        _conn.commit()
        return cur.rowcount == 1


def mark_alipay_closed(oid: str) -> None:
    with _lock:
        _conn.execute("""UPDATE orders SET status='failed', error='Payment was closed'
            WHERE id=? AND provider='alipay_web' AND status='unpaid'""", (oid,))
        _conn.commit()


def reserve_alipay_query(oid: str, interval: float = 30) -> bool:
    now = time.time()
    with _lock:
        cur = _conn.execute("""UPDATE orders SET last_query_at=?
            WHERE id=? AND provider='alipay_web' AND status='unpaid'
              AND (last_query_at IS NULL OR last_query_at<?)""", (now, oid, now - interval))
        _conn.commit()
        return cur.rowcount == 1


def reserve_refund(oid: str, request_no: str, amount_yuan: str) -> bool:
    """Reserve refund capacity atomically, including concurrent partial refunds."""
    from decimal import Decimal

    with _lock:
        row = _conn.execute("SELECT * FROM alipay_refunds WHERE out_request_no=?", (request_no,)).fetchone()
        if row:
            if row["out_trade_no"] != oid or row["amount_yuan"] != amount_yuan:
                raise ValueError("Refund request number was already used with different parameters")
            return False
        order = _conn.execute("SELECT amount_yuan, status, provider, paid_at FROM orders WHERE id=?", (oid,)).fetchone()
        if not order or order["provider"] != "alipay_web" or order["status"] not in (
                "paid", "processing", "done", "failed") or not order["paid_at"]:
            raise ValueError("Only paid Alipay orders can be refunded")
        reserved = _conn.execute("""SELECT amount_yuan FROM alipay_refunds
            WHERE out_trade_no=? AND status!='failed'""", (oid,)).fetchall()
        total = sum((Decimal(item[0]) for item in reserved), Decimal("0.00"))
        if total + Decimal(amount_yuan) > Decimal(order["amount_yuan"]):
            raise ValueError("Refund exceeds original payment")
        _conn.execute("INSERT INTO alipay_refunds VALUES (?,?,?,?,?)",
                      (request_no, oid, amount_yuan, "pending", time.time()))
        _conn.commit()
        return True


def get_refund(request_no: str) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM alipay_refunds WHERE out_request_no=?", (request_no,)).fetchone()
    return dict(row) if row else None


def refunded_total(oid: str) -> str:
    from decimal import Decimal
    with _lock:
        rows = _conn.execute("SELECT amount_yuan FROM alipay_refunds WHERE out_trade_no=? AND status!='failed'",
                             (oid,)).fetchall()
    return str(sum((Decimal(row[0]) for row in rows), Decimal("0.00")))


def set_refund_status(request_no: str, status: str) -> None:
    with _lock:
        _conn.execute("UPDATE alipay_refunds SET status=? WHERE out_request_no=?", (status, request_no))
        _conn.commit()


def expired_alipay_uploads(cutoff: float) -> list[dict]:
    with _lock:
        rows = _conn.execute("""SELECT id, video, photo FROM orders
            WHERE provider='alipay_web' AND created<?
              AND status IN ('unpaid', 'done', 'failed')
              AND (video IS NOT NULL OR photo IS NOT NULL)""", (cutoff,)).fetchall()
    return [dict(row) for row in rows]


def clear_expired_alipay_uploads(oid: str) -> None:
    with _lock:
        _conn.execute("""UPDATE orders SET video=NULL, photo=NULL,
            status=CASE WHEN status='unpaid' THEN 'failed' ELSE status END
            WHERE id=? AND provider='alipay_web'""", (oid,))
        _conn.commit()


def expired_results(cutoff: float) -> list[str]:
    """Done orders whose download period ended before cutoff and whose files are still on disk.
    Orders finished before done_at existed have none and are left alone."""
    with _lock:
        rows = _conn.execute("""SELECT id FROM orders WHERE status='done' AND done_at IS NOT NULL
            AND done_at<? AND files_deleted_at IS NULL""", (cutoff,)).fetchall()
    return [row["id"] for row in rows]


def unsent_ready_notices(since: float) -> list[str]:
    with _lock:
        rows = _conn.execute("""SELECT id FROM orders WHERE status='done' AND email IS NOT NULL
            AND notify_token IS NOT NULL AND notified_at IS NULL AND done_at>?""", (since,)).fetchall()
    return [row["id"] for row in rows]


def claim_notice(oid: str) -> bool:
    """Only one sender gets an order's ready email, even if the worker and the hourly retry race."""
    with _lock:
        cur = _conn.execute("UPDATE orders SET notified_at=? WHERE id=? AND notified_at IS NULL", (time.time(), oid))
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
