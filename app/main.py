"""Moment Manga web server: serves the page, takes uploads, creates payment QR codes,
and hands paid orders to the background worker."""
import shutil
from pathlib import Path

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
    return {"price": settings.PRICE_LABEL, "methods": provider.methods, "max_upload_mb": settings.MAX_UPLOAD_MB}


@app.post("/api/orders")
async def create_order(video: UploadFile = File(...), theme: str = Form(...), style: str = Form(...),
                       lang: str = Form("en")):
    if theme not in THEMES or style not in STYLES:
        raise HTTPException(400, "Unknown theme or style")
    if not (video.content_type or "").startswith("video/"):
        raise HTTPException(400, "Upload a video file")
    uploads = settings.DATA_DIR / "uploads"
    uploads.mkdir(exist_ok=True)
    tmp = uploads / f"incoming-{id(video)}"
    limit, size = settings.MAX_UPLOAD_MB * 1024 * 1024, 0
    with tmp.open("wb") as out:
        while chunk := await video.read(1 << 20):
            size += len(chunk)
            if size > limit:
                out.close(); tmp.unlink(missing_ok=True)
                raise HTTPException(413, f"Video is larger than {settings.MAX_UPLOAD_MB} MB")
            out.write(chunk)
    order = db.create(theme, style, "", "zh" if lang == "zh" else "en")
    path = uploads / f"{order['id']}{Path(video.filename or '').suffix[:6] or '.mp4'}"
    shutil.move(tmp, path)
    pay_url, ref = provider.create(order)
    db.update(order["id"], video=str(path), provider=settings.PAYMENT_PROVIDER, provider_ref=ref, pay_url=pay_url)
    return {"id": order["id"], "pay_url": pay_url, "status": "unpaid"}


class Options(BaseModel):
    theme: str
    style: str


@app.post("/api/orders/{oid}/options")
def change_options(oid: str, body: Options):
    """The dials can still be turned after upload, until the order is paid."""
    o = db.get(oid)
    if not o or o["status"] != "unpaid" or body.theme not in THEMES or body.style not in STYLES:
        raise HTTPException(409, "Order can no longer be changed")
    db.update(oid, theme=body.theme, style=body.style)
    return {"ok": True}


@app.get("/api/orders/{oid}")
def order_status(oid: str):
    o = db.get(oid)
    if not o:
        raise HTTPException(404)
    out = {"id": oid, "status": o["status"], "progress": o["progress"]}
    if o["status"] == "done":
        out["result_url"] = f"/api/orders/{oid}/manga.pdf"
    return out


@app.get("/api/orders/{oid}/manga.pdf")
def download(oid: str):
    o = db.get(oid)
    if not o or o["status"] != "done":
        raise HTTPException(404)
    return FileResponse(o["result"], media_type="application/pdf", filename="moment-manga.pdf")


@app.post("/api/pay/notify")
async def pay_notify(request: Request):
    oid, reply = await provider.notify(request)
    if oid:
        db.mark_paid(oid)
    if isinstance(reply, str):
        return PlainTextResponse(reply)
    return JSONResponse(reply)


if settings.PAYMENT_PROVIDER == "demo":
    @app.get("/api/dev/pay/{oid}", response_class=HTMLResponse)
    def dev_pay(oid: str):
        """Demo mode only: scanning the QR with a phone 'pays' the order."""
        if not db.mark_paid(oid) and not db.get(oid):
            raise HTTPException(404)
        return "<h1 style='font-family:sans-serif'>Demo payment received ✓</h1>"
