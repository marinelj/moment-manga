"""Moment Manga web server: serves the page, takes uploads, creates payment QR codes,
and hands paid orders to the background worker."""
import shutil
import hashlib
import json
import math
import secrets
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import db, payments, settings, worker

THEMES = {"sports", "travel", "portrait", "comedy"}
STYLES = {"miyazaki", "naruto", "slamdunk", "demonslayer", "onepiece"}
WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Moment Manga")
provider = payments.load()
app.mount("/demo", StaticFiles(directory=WEB / "demo"), name="demo")


def checkout_ready() -> bool:
    """Do not offer paid orders when the PDF worker cannot run."""
    return (settings.PAYMENT_PROVIDER == "alipay_web" and bool(settings.GEMINI_API_KEY)
            and bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")))


def validate_video(path: Path) -> None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, "Video validation is unavailable") from exc
    if result.returncode:
        raise HTTPException(400, "Invalid video")
    try:
        details = json.loads(result.stdout)
        duration = float(details["format"]["duration"])
        video_stream = any(stream.get("codec_type") == "video"
                           for stream in details.get("streams", []))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "Invalid video") from exc
    if not video_stream or not math.isfinite(duration) or not 0.5 <= duration <= 120:
        raise HTTPException(400, "Video must be between 0.5 and 120 seconds")


@app.on_event("startup")
def start_worker():
    worker.start()


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(WEB / "index.html")


@app.get("/zh/", response_class=HTMLResponse)
def index_zh():
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8").replace('<html lang="en"', '<html lang="zh-CN"', 1))


@app.get("/api/config")
def config():
    ready = checkout_ready()
    return {"price": settings.PRICE_LABEL, "methods": provider.methods,
            "max_upload_mb": settings.MAX_UPLOAD_MB,
            "checkout_ready": ready,
            "products": list(provider.prices) if ready else [],
            "charges": {product: {"amount": amount, "currency": "CNY"}
                        for product, amount in provider.prices.items()}
            if ready else {}}


@app.post("/api/orders")
async def create_order(video: UploadFile = File(...), theme: str | None = Form(None),
                       style: str | None = Form(None), photo: UploadFile | None = File(None),
                       product: str | None = Form(None), lang: str = Form("en")):
    webpay = settings.PAYMENT_PROVIDER == "alipay_web"
    if webpay:
        if not checkout_ready():
            raise HTTPException(503, "Checkout is unavailable")
        if product not in provider.prices:
            raise HTTPException(400, "This product is not available for checkout")
        theme, style = "sports", "slamdunk"
    elif theme not in THEMES or style not in STYLES:
        raise HTTPException(400, "Unknown theme or style")
    if lang not in ("en", "zh"):
        raise HTTPException(400, "Unknown language")
    if not (video.content_type or "").startswith("video/"):
        raise HTTPException(400, "Upload a video file")
    if webpay and Path(video.filename or "").suffix.lower() not in (".mp4", ".mov", ".webm"):
        raise HTTPException(400, "Unsupported video format")
    if photo and (photo.content_type not in ("image/jpeg", "image/png", "image/webp")
                  or Path(photo.filename or "").suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(400, "Unsupported photo format")
    uploads = settings.DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    tmp = uploads / f"incoming-{secrets.token_hex(16)}"
    tmp_photo = uploads / f"incoming-{secrets.token_hex(16)}-photo" if photo else None
    try:
        limit, size = settings.MAX_UPLOAD_MB * 1024 * 1024, 0
        with tmp.open("xb") as out:
            while chunk := await video.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Video is larger than {settings.MAX_UPLOAD_MB} MB")
                out.write(chunk)
        if not size:
            raise HTTPException(400, "Video is empty")
        if webpay:
            validate_video(tmp)
        if photo and tmp_photo:
            photo_size = 0
            with tmp_photo.open("xb") as out:
                while chunk := await photo.read(1 << 20):
                    photo_size += len(chunk)
                    if photo_size > 20 * 1024 * 1024:
                        raise HTTPException(413, "Photo is larger than 20 MB")
                    out.write(chunk)
            try:
                with Image.open(tmp_photo) as img:
                    if img.format not in ("JPEG", "PNG", "WEBP"):
                        raise HTTPException(400, "Unsupported photo format")
                    if img.width * img.height > 40_000_000:
                        raise HTTPException(400, "Photo dimensions are too large")
                    img.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise HTTPException(400, "Invalid photo") from exc
        order = db.create(theme, style, "", lang)
        path = uploads / f"{order['id']}{Path(video.filename or '').suffix.lower() or '.mp4'}"
        shutil.move(tmp, path)
        photo_path = None
        if tmp_photo:
            photo_path = uploads / f"{order['id']}-photo{Path(photo.filename or '').suffix.lower()}"
            shutil.move(tmp_photo, photo_path)
    finally:
        tmp.unlink(missing_ok=True)
        if tmp_photo:
            tmp_photo.unlink(missing_ok=True)
    if webpay:
        token = secrets.token_urlsafe(32)
        db.update(order["id"], video=str(path), photo=str(photo_path) if photo_path else None,
                  provider="alipay_web", product=product, amount_yuan=provider.prices[product],
                  app_id=provider.app_id, seller_id=provider.seller_id,
                  buyer_token_hash=hashlib.sha256(token.encode()).hexdigest())
        result = JSONResponse({"id": order["id"], "status": "unpaid", "payment": provider.payment(db.get(order["id"]))})
        result.set_cookie(f"mm_buyer_{order['id']}", token, httponly=True, samesite="lax",
                          secure=settings.PUBLIC_URL.startswith("https://"), path="/api", max_age=30 * 86400)
        return result
    pay_url, ref = provider.create(order)
    db.update(order["id"], video=str(path), provider=settings.PAYMENT_PROVIDER, provider_ref=ref, pay_url=pay_url)
    return {"id": order["id"], "pay_url": pay_url, "status": "unpaid"}


class Options(BaseModel):
    theme: str
    style: str


@app.post("/api/orders/{oid}/options")
def change_options(oid: str, body: Options):
    """The dials can still be turned after upload, until the order is paid."""
    if settings.PAYMENT_PROVIDER == "alipay_web":
        raise HTTPException(404)
    o = db.get(oid)
    if not o or o["status"] != "unpaid" or body.theme not in THEMES or body.style not in STYLES:
        raise HTTPException(409, "Order can no longer be changed")
    db.update(oid, theme=body.theme, style=body.style)
    return {"ok": True}


@app.get("/api/orders/{oid}")
def order_status(oid: str, request: Request):
    o = db.get(oid)
    if not o:
        raise HTTPException(404)
    if o.get("provider") == "alipay_web":
        _require_buyer(request, o)
        if o["status"] == "unpaid" and db.reserve_alipay_query(oid):
            try:
                provider.query(o)
                o = db.get(oid)
            except Exception:
                pass  # A transient gateway failure is an unknown payment state.
    out = {"id": oid, "status": o["status"], "progress": o["progress"]}
    if o["status"] == "done":
        out["downloads"] = {"book": f"/api/orders/{oid}/manga.pdf"}
        out["result_url"] = out["downloads"]["book"]
    if o.get("provider") == "alipay_web" and o["status"] == "unpaid":
        out["payment"] = provider.payment(o)
    return out


@app.get("/api/orders/{oid}/manga.pdf")
def download(oid: str, request: Request):
    o = db.get(oid)
    if not o or o["status"] != "done":
        raise HTTPException(404)
    if o.get("provider") == "alipay_web":
        _require_buyer(request, o)
    return FileResponse(o["result"], media_type="application/pdf", filename="moment-manga.pdf")


@app.post("/api/pay/notify")
async def pay_notify(request: Request):
    if settings.PAYMENT_PROVIDER == "alipay_web":
        return PlainTextResponse(await provider.notify(request))
    oid, reply = await provider.notify(request)
    if oid:
        db.mark_paid(oid)
    if isinstance(reply, str):
        return PlainTextResponse(reply)
    return JSONResponse(reply)


def _require_buyer(request: Request, order: dict) -> None:
    token = request.cookies.get(f"mm_buyer_{order['id']}", "")
    actual = hashlib.sha256(token.encode()).hexdigest()
    if not token or not secrets.compare_digest(actual, order.get("buyer_token_hash") or ""):
        raise HTTPException(403, "Order access denied")


@app.get("/api/orders/{oid}/pay", response_class=HTMLResponse)
def alipay_pay(oid: str, request: Request):
    if settings.PAYMENT_PROVIDER != "alipay_web":
        raise HTTPException(404)
    order = db.get(oid)
    if not order:
        raise HTTPException(404)
    _require_buyer(request, order)
    try:
        form = provider.page_form(order)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return HTMLResponse(form, headers={"Cache-Control": "no-store",
                                       "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; form-action https://openapi.alipay.com https://openapi-sandbox.dl.alipaydev.com"})


@app.get("/api/pay/return", response_class=HTMLResponse)
def alipay_return(request: Request):
    oid = None
    if settings.PAYMENT_PROVIDER == "alipay_web" and request.query_params:
        params = dict(request.query_params)
        candidate = db.get(params.get("out_trade_no", ""))
        if candidate and provider.verify_signature(params):
            try:
                _require_buyer(request, candidate)
                oid = candidate["id"]
            except HTTPException:
                pass
    script = ""
    if oid:
        script = ("<script>setInterval(async()=>{try{const r=await fetch('/api/orders/" + oid +
                  "',{cache:'no-store'});if(!r.ok)return;const x=await r.json();"
                  "if(x.status==='paid'||x.status==='processing'||x.status==='done')"
                  "document.getElementById('state').textContent='Payment confirmed. Return to Moment Manga.';"
                  "}catch{}},3000)</script>")
    return HTMLResponse("<!doctype html><html><head><meta charset='utf-8'><title>Payment status</title></head>"
                        "<body><main><h1>Payment status</h1><p id='state'>Confirming payment. "
                        "Return to Moment Manga to see your order status.</p></main>" + script + "</body></html>",
                        headers={"Cache-Control": "no-store"})


def _require_admin(request: Request) -> None:
    if settings.PAYMENT_PROVIDER != "alipay_web":
        raise HTTPException(404)
    expected = settings.env("ALIPAY_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(503, "Payment administration is not configured")
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(403)


class RefundRequest(BaseModel):
    amount: str
    out_request_no: str


@app.post("/api/admin/alipay/orders/{oid}/query")
def admin_alipay_query(oid: str, request: Request):
    _require_admin(request)
    order = db.get(oid)
    if not order or order.get("provider") != "alipay_web":
        raise HTTPException(404)
    result = provider.query(order)
    return {"code": result.get("code"), "trade_status": result.get("trade_status"),
            "order_status": db.get(oid)["status"]}


@app.post("/api/admin/alipay/orders/{oid}/refund")
def admin_alipay_refund(oid: str, body: RefundRequest, request: Request):
    _require_admin(request)
    order = db.get(oid)
    if not order or order.get("provider") != "alipay_web":
        raise HTTPException(404)
    try:
        result = provider.refund(order, body.out_request_no, body.amount)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": result.get("code"), "fund_change": result.get("fund_change"),
            "refund_status": "confirmed" if result.get("fund_change") == "Y" else "query_required"}


@app.post("/api/admin/alipay/orders/{oid}/refunds/{request_no}/query")
def admin_alipay_refund_query(oid: str, request_no: str, request: Request):
    _require_admin(request)
    order = db.get(oid)
    if not order or order.get("provider") != "alipay_web":
        raise HTTPException(404)
    try:
        result = provider.refund_query(order, request_no)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": result.get("code"), "refund_status": result.get("refund_status")}


@app.post("/api/admin/alipay/orders/{oid}/close")
def admin_alipay_close(oid: str, request: Request):
    _require_admin(request)
    order = db.get(oid)
    if not order or order.get("provider") != "alipay_web":
        raise HTTPException(404)
    try:
        result = provider.close(order)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": result.get("code")}


if settings.PAYMENT_PROVIDER == "demo":
    @app.get("/api/dev/pay/{oid}", response_class=HTMLResponse)
    def dev_pay(oid: str):
        """Demo mode only: scanning the QR with a phone 'pays' the order."""
        if not db.mark_paid(oid) and not db.get(oid):
            raise HTTPException(404)
        return "<h1 style='font-family:sans-serif'>Demo payment received ✓</h1>"
