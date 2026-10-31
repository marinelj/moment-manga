"""Background worker: one thread that turns paid orders into manga, one at a time.
For more throughput run several workers or move this to a job queue."""
import threading
import time
import traceback
from pathlib import Path

from . import db, pipeline, settings


def _cleanup_uploads():
    uploads = (settings.DATA_DIR / "uploads").resolve()
    for order in db.expired_alipay_uploads(time.time() - 7 * 86400):
        for filename in (order["video"], order["photo"]):
            if filename:
                path = Path(filename).resolve()
                if path.parent == uploads:
                    path.unlink(missing_ok=True)
        db.clear_expired_alipay_uploads(order["id"])


def _loop():
    db.requeue_stuck()
    last_cleanup = 0
    while True:
        if time.time() - last_cleanup > 3600:
            try:
                _cleanup_uploads()
            except OSError:
                traceback.print_exc()
            last_cleanup = time.time()
        order = db.claim_next()
        if not order:
            time.sleep(2)
            continue
        try:
            pdf = pipeline.run(order, progress=lambda p: db.update(order["id"], progress=int(p)))
            db.update(order["id"], status="done", progress=100, result=str(pdf))
        except Exception:
            traceback.print_exc()
            db.update(order["id"], status="failed", error=traceback.format_exc()[-2000:])


def start():
    threading.Thread(target=_loop, daemon=True, name="manga-worker").start()
