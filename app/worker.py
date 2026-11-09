"""Background worker: one thread that turns paid orders into manga, one at a time.
For more throughput run several workers or move this to a job queue.

An order in the "batch" API style waits on a Gemini Batch job for up to BATCH_MAX_WAIT_HOURS, so it gets
its own thread and doesn't hold up the queue. Live video work still runs one order at a time."""
import contextlib
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import db, modes, notify, pipeline, sample, settings, video

_live = threading.Lock()     # paid interactive Gemini work: one order at a time, as before batch existed


def _cleanup_uploads():
    uploads = (settings.DATA_DIR / "uploads").resolve()
    for order in db.expired_alipay_uploads(time.time() - 7 * 86400):
        for filename in (order["video"], order["photo"]):
            if filename:
                path = Path(filename).resolve()
                if path.parent == uploads:
                    path.unlink(missing_ok=True)
        db.clear_expired_alipay_uploads(order["id"])


def _expire_results(now: float | None = None) -> None:
    """Delete finished files DOWNLOAD_DAYS after the order is done (the page and the email say so)."""
    jobs = (settings.DATA_DIR / "jobs").resolve()
    for oid in db.expired_results((now or time.time()) - settings.DOWNLOAD_DAYS * 86400):
        folder = (jobs / oid).resolve()
        if folder.parent == jobs:
            shutil.rmtree(folder, ignore_errors=True)
        db.update(oid, files_deleted_at=time.time(), email=None, notify_token=None)


def _loop():
    db.requeue_stuck()
    last_cleanup = 0
    while True:
        if time.time() - last_cleanup > 3600:
            try:
                _cleanup_uploads()
                _expire_results()
            except OSError:
                traceback.print_exc()
            notify.send_pending()          # ready emails that failed earlier get another try
            last_cleanup = time.time()
        order = db.claim_next()
        if not order:
            time.sleep(2)
            continue
        if waits_on_batch(order):
            threading.Thread(target=process, args=(order,), daemon=True, name="batch-" + order["id"][:8]).start()
        else:
            process(order)


def _generation(order: dict) -> str:
    # The mode recorded when the order was created wins, so a test order never reaches the paid
    # pipelines even if the server restarts with GENERATION_MODE=live. Older orders use the setting.
    return order.get("generation") or modes.current()


def waits_on_batch(order: dict) -> bool:
    return _generation(order) == "live" and order.get("api_style") == "batch"


def process(order: dict) -> None:
    """Book only, or book then moving-manga video for the "all" package."""
    oid, with_video = order["id"], order.get("product") == "all"
    book_share = 30 if with_video else 100
    if _generation(order) == "sample":
        try:
            pdf, mp4 = sample.run(order, with_video)
            db.update(oid, result=str(pdf), video_result=str(mp4) if mp4 else None, status="done", progress=100,
                      done_at=time.time())
        except Exception:
            traceback.print_exc()
            db.update(oid, status="failed", error=traceback.format_exc()[-2000:])
            return
        notify.order_ready(oid)            # payment tests get the email too, so it can be tried for free
        return
    try:
        # A batch order waits on Google without the lock; the standard calls around it are few and short.
        with contextlib.nullcontext() if waits_on_batch(order) else _live:
            pdf = pipeline.run(order, progress=lambda p: db.update(oid, progress=int(p * book_share / 100)))
        db.update(oid, result=str(pdf))
        if with_video:
            with _live:
                mp4 = video.run(order, progress=lambda p: db.update(oid, progress=int(30 + p * 0.7)))
            db.update(oid, video_result=str(mp4))
        db.update(oid, status="done", progress=100, done_at=time.time())
    except Exception:
        traceback.print_exc()
        db.update(oid, status="failed", error=traceback.format_exc()[-2000:])
        return
    notify.order_ready(oid)


def start():
    threading.Thread(target=_loop, daemon=True, name="manga-worker").start()
