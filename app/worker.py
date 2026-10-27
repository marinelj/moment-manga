"""Background worker: one thread that turns paid orders into manga, one at a time.
For more throughput run several workers or move this to a job queue."""
import threading
import time
import traceback

from . import db, pipeline


def _loop():
    db.requeue_stuck()
    while True:
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
