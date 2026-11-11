"""Moment Manga web server: serves the page, takes uploads, creates payment QR codes,
and hands paid orders to the background worker."""
import shutil
import hashlib
import json
import math
import re
import secrets
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import db, director, modes, notify, payments, settings, worker

THEMES = {"sports", "travel", "portrait", "comedy"}
STYLES = {"miyazaki", "naruto", "slamdunk", "demonslayer", "onepiece"}
WEB = Path(__file__).resolve().parent.parent / "web"
EMAIL = re.compile(r"[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"'.]{2,}")   # a sanity check, not full RFC 5322

app = FastAPI(title="Moment Manga")
if settings.CORS_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware
    # Only the listed sites may call the API from a browser. Buyers prove access with the
    # X-Buyer-Token header (cross-site cookies are blocked by Safari), admins with their Bearer token.
    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=True,
                       allow_methods=["GET", "POST"], allow_headers=["authorization", "content-type", "x-buyer-token"])
provider = payments.load()
app.mount("/demo", StaticFiles(directory=WEB / "demo"), name="demo")


@app.get("/healthz")
def healthz():
    return {"ok": True}


def checkout_ready() -> bool:
    """Do not offer paid orders when the worker cannot fulfil them."""
    mode = modes.current()
    if settings.PAYMENT_PROVIDER != "alipay_web" or mode not in ("live", "sample"):
        return False
    if mode == "sample" and settings.ALIPAY_ENV == "production" and not settings.ALLOW_PRODUCTION_PAYMENT_TEST:
        return False          # real money for sample files only with the explicit test flag
    tools = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    return tools and (mode == "sample" or bool(settings.GEMINI_API_KEY))


def validate_video(path: Path) -> float:
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
    return duration


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
            "video_max_seconds": settings.VIDEO_MAX_SECONDS,
            "checkout_ready": ready,
            "generation": modes.current(),
            # batch: real orders can take up to batch_max_wait_hours, so tell the buyer before they pay
            "api_style": modes.api_style(),
            "batch_max_wait_hours": settings.BATCH_MAX_WAIT_HOURS,
            "download_days": settings.DOWNLOAD_DAYS,      # finished files are deleted after this many days
            "ready_email": notify.enabled(),               # show the optional email field only when true
            "products": list(provider.prices) if ready else [],
            "charges": {product: {"amount": amount, "currency": "CNY"}
                        for product, amount in provider.prices.items()}
            if ready else {}}


@app.post("/api/orders")
async def create_order(video: UploadFile = File(...), theme: str | None = Form(None),
                       style: str | None = Form(None), photo: UploadFile | None = File(None),
                       product: str | None = Form(None), lang: str = Form("en"), email: str | None = Form(None)):
    webpay = settings.PAYMENT_PROVIDER == "alipay_web"
    email = (email or "").strip() or None
    if email and (len(email) > 254 or not EMAIL.fullmatch(email)):
        raise HTTPException(400, "Invalid email address")
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
            duration = validate_video(tmp)
            if product == "all" and duration and duration > settings.VIDEO_MAX_SECONDS:
                raise HTTPException(400, f"The redrawn video needs a clip of at most "
                                         f"{settings.VIDEO_MAX_SECONDS:g} seconds")
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
        # Fixed for the order's lifetime, whatever the admin page says later.
        db.update(order["id"], generation=modes.current(), api_style=modes.api_style())
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
        if email and notify.enabled():
            # Kept only until the ready email is sent (token) or the files are deleted (address).
            db.update(order["id"], email=email, notify_token=token)
        result = JSONResponse({"id": order["id"], "status": "unpaid", "payment": provider.payment(db.get(order["id"])),
                               "buyer_token": token})
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
    if o.get("generation") == "sample":
        out["payment_test"] = True    # sample files only: the page should say this is a payment test
    elif o.get("api_style") == "batch":
        out["api_style"] = "batch"    # real Gemini Batch order: ready within batch_max_wait_hours, often sooner
    if o["status"] == "done" and o.get("files_deleted_at"):
        out["files_expired"] = True   # past the download period: the files are gone
    elif o["status"] == "done":
        if o.get("done_at"):
            out["files_expire_at"] = int(notify.expires_at(o))   # Unix seconds; show it as a date
        out["downloads"] = {"book": f"/api/orders/{oid}/manga.pdf"}
        if o.get("video_result"):
            out["downloads"]["clip"] = f"/api/orders/{oid}/manga.mp4"
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
    _still_kept(o)
    return FileResponse(o["result"], media_type="application/pdf", filename="moment-manga.pdf")


@app.get("/api/orders/{oid}/manga.mp4")
def download_video(oid: str, request: Request):
    o = db.get(oid)
    if not o or o["status"] != "done" or not o.get("video_result"):
        raise HTTPException(404)
    if o.get("provider") == "alipay_web":
        _require_buyer(request, o)
    _still_kept(o)
    return FileResponse(o["video_result"], media_type="video/mp4", filename="moment-manga.mp4")


def _still_kept(o: dict) -> None:
    if o.get("files_deleted_at"):
        raise HTTPException(410, f"Files are deleted {settings.DOWNLOAD_DAYS} days after the order is ready")


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
    # Same-site pages use the cookie; a page on another origin sends the token from the order
    # response as X-Buyer-Token, or as ?t= on links (payment page, downloads).
    token = (request.cookies.get(f"mm_buyer_{order['id']}", "") or request.headers.get("x-buyer-token", "")
             or request.query_params.get("t", ""))
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
    # form-action also covers redirects: Alipay's gateway 302s the POST to its cashier
    # (e.g. excashier.alipay.com), so allow the whole Alipay domain, not just the gateway host.
    return HTMLResponse(form, headers={"Cache-Control": "no-store",
                                       "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; form-action https://*.alipay.com https://*.alipaydev.com"})


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
    expected = settings.env("ADMIN_TOKEN") or settings.env("ALIPAY_ADMIN_TOKEN")  # old name still accepted
    if not expected:
        raise HTTPException(503, "Payment administration is not configured")
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(403)


def _require_debug() -> None:
    """The debug page and its routes are open, with no token (owner's choice, 2026-11-03).
    Refund and other Alipay admin routes still use _require_admin."""
    if settings.PAYMENT_PROVIDER != "alipay_web":
        raise HTTPException(404)


class RefundRequest(BaseModel):
    amount: str
    out_request_no: str


class GenerationChoice(BaseModel):
    mode: str | None     # "live", "sample", or null to go back to the GENERATION_MODE setting


class ApiStyleChoice(BaseModel):
    style: str | None    # "standard", "batch", or null to go back to the GEMINI_API_STYLE setting


class DirectorChoice(BaseModel):
    director: str | None    # "auto", "claude", "grok", "off", or null to go back to the BOOK_DIRECTOR setting


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Debug page: no token. Anyone with the address can switch mocked/real Gemini."""
    return HTMLResponse((Path(__file__).resolve().parent / "admin.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/api/admin/status")
def admin_status(request: Request):
    _require_debug()
    override = db.get_setting(modes.KEY)
    style_override = db.get_setting(modes.STYLE_KEY)
    return {"mode": modes.current(), "mode_source": "admin page" if override else "GENERATION_MODE setting",
            "env_mode": settings.GENERATION_MODE, "alipay_env": settings.ALIPAY_ENV,
            "api_style": modes.api_style(),
            "api_style_source": "admin page" if style_override else "GEMINI_API_STYLE setting",
            "batch_max_wait_hours": settings.BATCH_MAX_WAIT_HOURS,
            "checkout_ready": checkout_ready(), "gemini_key_set": bool(settings.GEMINI_API_KEY),
            # Who plans and checks a real book: "claude" or "grok" (directed book), else "gemini" (the older
            # Gemini-only book). book_director_choice is the switch behind it: auto, claude, grok or off.
            "book_director": director.provider() or "gemini",
            "book_director_choice": modes.book_director(),
            "book_director_source": "admin page" if db.get_setting(modes.DIRECTOR_KEY) else "BOOK_DIRECTOR setting",
            "director_model": "grok-bot" if director.provider() == "grok" else settings.DIRECTOR_MODEL,
            "anthropic_key_set": bool(settings.ANTHROPIC_API_KEY),
            "grok_director_url_set": bool(settings.GROK_DIRECTOR_URL),
            "production_payment_test_allowed": settings.ALLOW_PRODUCTION_PAYMENT_TEST,
            "prices": dict(getattr(provider, "prices", {})),
            "orders": db.recent_orders(20)}


@app.post("/api/admin/generation")
def admin_set_generation(body: GenerationChoice, request: Request):
    _require_debug()
    try:
        modes.set_override(body.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"mode": modes.current(), "checkout_ready": checkout_ready()}


@app.post("/api/admin/api-style")
def admin_set_api_style(body: ApiStyleChoice, request: Request):
    _require_debug()
    try:
        modes.set_api_style(body.style)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"api_style": modes.api_style()}


@app.post("/api/admin/book-director")
def admin_set_book_director(body: DirectorChoice, request: Request):
    _require_debug()
    try:
        modes.set_book_director(body.director)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"book_director": director.provider() or "gemini", "book_director_choice": modes.book_director()}


@app.get("/api/director/bundles/{token}/bundle.zip")
def director_bundle(token: str):
    """The pictures of one question to a director that cannot be sent pictures (director.GrokDirector). The
    token is the only key: 32 random characters that exist only while that question is open."""
    path = settings.DIRECTOR_BUNDLE_DIR / token / "bundle.zip"
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token) or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="application/zip",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/api/admin/orders/{oid}/costs")
def admin_order_costs(oid: str, request: Request):
    """What an order cost us in model calls, line by line."""
    _require_debug()
    order = db.get(oid)
    if not order:
        raise HTTPException(404)
    lines = db.order_costs(oid)
    return {"id": oid, "product": order.get("product"), "status": order["status"],
            "error": (order.get("error") or "")[-300:], "total_usd": round(db.order_cost_total(oid), 4),
            "lines": [{k: line[k] for k in ("step", "model", "units", "usd", "cached")} for line in lines]}


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
