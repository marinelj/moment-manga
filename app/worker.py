"""Background worker: one thread that turns paid orders into manga, one at a time.
For more throughput run several workers or move this to a job queue."""
import threading
import time
import traceback
from pathlib import Path

from . import db, modes, pipeline, sample, settings, video


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
        process(order)


def process(order: dict) -> None:
    """Book only, or book then moving-manga video for the "all" package."""
    oid, with_video = order["id"], order.get("product") == "all"
    book_share = 30 if with_video else 100
    # The mode recorded when the order was created wins, so a test order never reaches the paid
    # pipelines even if the server restarts with GENERATION_MODE=live. Older orders use the setting.
    if (order.get("generation") or modes.current()) == "sample":
        try:
            pdf, mp4 = sample.run(order, with_video)
            db.update(oid, result=str(pdf), video_result=str(mp4) if mp4 else None, status="done", progress=100)
        except Exception:
            traceback.print_exc()
            db.update(oid, status="failed", error=traceback.format_exc()[-2000:])
        return
    try:
        pdf = pipeline.run(order, progress=lambda p: db.update(oid, progress=int(p * book_share / 100)))
        db.update(oid, result=str(pdf))
        if with_video:
            mp4 = video.run(order, progress=lambda p: db.update(oid, progress=int(30 + p * 0.7)))
            db.update(oid, video_result=str(mp4))
        db.update(oid, status="done", progress=100)
    except Exception:
        traceback.print_exc()
        db.update(oid, status="failed", error=traceback.format_exc()[-2000:])


def start():
    threading.Thread(target=_loop, daemon=True, name="manga-worker").start()
