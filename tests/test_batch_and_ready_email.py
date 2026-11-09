"""Gemini API style switch (standard / batch), the batch book path, the ready email and the download period."""
import base64
import io
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, main, modes, notify, pipeline, settings, worker

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg is not installed")


class FakeAlipay:
    methods = ["alipay"]
    prices = {"book": "0.01"}
    app_id = "app"
    seller_id = "seller"

    def payment(self, order):
        return {"pay_url": f"/api/orders/{order['id']}/pay", "charge": {"amount": order["amount_yuan"], "currency": "CNY"}}

    def query(self, order):
        return {"code": "40004"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for (sql,) in db._conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        conn.execute(sql)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "GEMINI_API_STYLE", "standard")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    yield tmp_path
    conn.close()


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(settings, "ALIPAY_ENV", "sandbox")
    monkeypatch.setattr(settings, "GENERATION_MODE", "sample")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(main, "provider", FakeAlipay())
    monkeypatch.setattr(main, "validate_video", lambda path: 10.0)
    return TestClient(main.app)


def new_order(client, **extra):
    return client.post("/api/orders", files={"video": ("a.mp4", b"v", "video/mp4")},
                       data={"product": "book", "lang": "en", **extra})


# ---------- the switch ----------

def test_api_style_switch_applies_to_new_orders_only(api):
    modes.set_override("live")
    first = new_order(api).json()
    assert api.post("/api/admin/api-style", json={"style": "batch"}).json()["api_style"] == "batch"
    second = new_order(api).json()
    assert db.get(first["id"])["api_style"] == "standard" and db.get(second["id"])["api_style"] == "batch"

    status = api.get("/api/admin/status").json()
    assert status["api_style"] == "batch" and status["api_style_source"] == "admin page"
    assert {o["id"]: o["api_style"] for o in status["orders"]} == {first["id"]: "standard", second["id"]: "batch"}
    config = api.get("/api/config").json()
    assert config["api_style"] == "batch" and config["batch_max_wait_hours"] == settings.BATCH_MAX_WAIT_HOURS

    headers = {"x-buyer-token": second["buyer_token"]}
    assert api.get(f"/api/orders/{second['id']}", headers=headers).json()["api_style"] == "batch"
    headers = {"x-buyer-token": first["buyer_token"]}
    assert "api_style" not in api.get(f"/api/orders/{first['id']}", headers=headers).json()

    assert api.post("/api/admin/api-style", json={"style": None}).json()["api_style"] == "standard"
    assert api.post("/api/admin/api-style", json={"style": "cheap"}).status_code == 400


def test_only_real_batch_orders_wait_in_their_own_thread():
    assert worker.waits_on_batch({"generation": "live", "api_style": "batch"})
    assert not worker.waits_on_batch({"generation": "live", "api_style": "standard"})
    assert not worker.waits_on_batch({"generation": "sample", "api_style": "batch"})


# ---------- the batch book path ----------

def _png(shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1024, 768), (shade,) * 3).save(buf, "PNG")
    return buf.getvalue()


class FakeGemini:
    """Standard calls and a Batch API job, shaped like the SDK objects the pipeline reads."""

    PLAN = {"title": "t", "pages": [
        {"panels": [{"frame": i, "shot": "wide", "description": "d", "caption": "c", "sfx": ""} for i in range(3)]},
        {"panels": [{"frame": i, "shot": "close-up", "description": "d", "caption": "", "sfx": "!"} for i in range(4)]}]}

    def __init__(self, states=("JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"), missing=1, errors=1):
        self.states, self.missing, self.errors = list(states), missing, errors
        self.storyboards = self.images = self.created = 0
        self.cancelled, self.requests = [], []
        self.models = SimpleNamespace(generate_content=self._generate)
        self.files = SimpleNamespace(upload=self._upload, download=self._download)
        self.batches = SimpleNamespace(create=self._create, get=self._get,
                                       cancel=lambda name: self.cancelled.append(name))

    def _generate(self, model, contents, config):
        if config.response_modalities == ["IMAGE"]:
            self.images += 1
            part = SimpleNamespace(inline_data=SimpleNamespace(data=_png(200)))
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        self.storyboards += 1
        usage = SimpleNamespace(prompt_token_count=26_000, candidates_token_count=1_000, thoughts_token_count=1_000)
        return SimpleNamespace(text=json.dumps(self.PLAN), usage_metadata=usage)

    def _upload(self, file, config):
        assert config.mime_type == "jsonl"
        self.requests = [json.loads(line) for line in Path(file).read_text().splitlines()]
        return SimpleNamespace(name="files/requests")

    def _create(self, model, src, config):
        assert model == settings.GEMINI_IMAGE_MODEL and src == "files/requests"
        self.created += 1
        return SimpleNamespace(name="batches/1")

    def _get(self, name):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        done = state == "JOB_STATE_SUCCEEDED"
        return SimpleNamespace(state=SimpleNamespace(name=state),
                               dest=SimpleNamespace(file_name="files/results") if done else None)

    def _download(self, file):
        keys = [r["key"] for r in self.requests][:len(self.requests) - self.missing]
        lines = []
        for n, key in enumerate(keys):
            if n < self.errors:
                lines.append({"key": key, "error": {"code": 13, "message": "internal"}})
            else:
                data = base64.b64encode(_png(90)).decode()
                lines.append({"key": key, "response": {"candidates": [
                    {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": data}}]}}]}})
        return "\n".join(json.dumps(line) for line in lines).encode()


@pytest.fixture
def clip(store):
    path = store / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-t", "3", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def book_order(clip, style):
    return {"id": "order1", "video": str(clip), "theme": "sports", "style": "slamdunk", "lang": "en",
            "api_style": style}


def costs():
    """Panel lines only; the storyboard line is checked once below."""
    return sorted((line["step"], line["usd"]) for line in db.order_costs("order1") if line["step"] != "storyboard")


@needs_ffmpeg
def test_batch_book_costs_half_and_draws_what_batch_missed_on_standard(store, clip):
    fake = FakeGemini(missing=1, errors=1)
    pdf = pipeline.run(book_order(clip, "batch"), client=fake, sleep=lambda s: None, now=lambda: 0)
    assert pdf.exists() and fake.created == 1
    assert len(fake.requests) == 7
    request = fake.requests[0]["request"]
    assert request["generationConfig"] == {"responseModalities": ["IMAGE"]}
    assert request["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/jpeg"
    assert fake.images == 2                     # one request failed in the job, one never came back
    assert costs() == [("book panel", 0.140)] * 2 + [("book panel (batch)", 0.070)] * 5
    storyboard = [line for line in db.order_costs("order1") if line["step"] == "storyboard"]
    assert len(storyboard) == 1 and storyboard[0]["usd"] == pytest.approx(26_000 * 0.75e-6 + 2_000 * 3.75e-6)


@needs_ffmpeg
def test_restarted_batch_order_waits_on_the_same_job(store, clip):
    fake = FakeGemini(states=("JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"), missing=0, errors=0)

    def server_stops(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(book_order(clip, "batch"), client=fake, sleep=server_stops, now=lambda: 0)
    assert (store / "jobs" / "order1" / "batch.json").exists() and costs() == []

    pipeline.run(book_order(clip, "batch"), client=fake, sleep=lambda s: None, now=lambda: 0)
    assert fake.created == 1 and fake.storyboards == 1 and fake.images == 0
    assert costs() == [("book panel (batch)", 0.070)] * 7


@needs_ffmpeg
def test_batch_past_the_deadline_is_cancelled_and_drawn_on_standard(store, clip):
    fake = FakeGemini(states=("JOB_STATE_RUNNING",))
    clock = [0.0]
    pipeline.run(book_order(clip, "batch"), client=fake,
                 sleep=lambda s: clock.__setitem__(0, clock[0] + 30 * 3600), now=lambda: clock[0])
    assert fake.cancelled == ["batches/1"] and fake.images == 7
    assert costs() == [("book panel", 0.140)] * 7


@needs_ffmpeg
def test_standard_book_reuses_panels_already_drawn(store, clip):
    fake = FakeGemini()
    pipeline.run(book_order(clip, "standard"), client=fake)
    pipeline.run(book_order(clip, "standard"), client=fake)
    assert fake.created == 0 and fake.storyboards == 1 and fake.images == 7
    assert costs() == [("book panel", 0.140)] * 7


# ---------- ready email and download period ----------

@pytest.fixture
def mail(api, monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM", "shop@example.com")
    monkeypatch.setattr(notify, "_send", sent.append)
    return sent


def finish(oid):
    db.update(oid, status="paid")
    worker.process(db.get(oid))
    return db.get(oid)


def test_ready_email_has_the_order_link_and_goes_once(api, mail):
    assert api.get("/api/config").json()["ready_email"] is True
    created = new_order(api, email=" buyer@example.com ").json()
    oid, token = created["id"], created["buyer_token"]
    assert db.get(oid)["email"] == "buyer@example.com"

    order = finish(oid)
    assert order["status"] == "done" and order["notify_token"] is None and order["notified_at"]
    assert len(mail) == 1
    msg = mail[0]
    assert msg["To"] == "buyer@example.com" and msg["Subject"] == "[Test] Your Moment Manga is ready"
    body = msg.get_content()
    assert f"https://marinelj.github.io/moment-manga/?order={oid}&t={token}" in body
    assert "deleted 7 days after" in body

    assert notify.order_ready(oid) is False and len(mail) == 1
    status = api.get(f"/api/orders/{oid}", headers={"x-buyer-token": token}).json()
    assert status["files_expire_at"] == int(order["done_at"] + 7 * 86400)


def test_chinese_order_links_to_the_chinese_page(api, mail):
    created = api.post("/api/orders", files={"video": ("a.mp4", b"v", "video/mp4")},
                       data={"product": "book", "lang": "zh", "email": "a@qq.com"}).json()
    finish(created["id"])
    assert "/moment-manga/zh/?order=" in mail[0].get_content() and "已完成" in mail[0]["Subject"]


def test_no_mail_server_means_no_address_kept_and_bad_addresses_are_refused(api):
    assert api.get("/api/config").json()["ready_email"] is False
    created = new_order(api, email="buyer@example.com").json()
    assert db.get(created["id"])["email"] is None and db.get(created["id"])["notify_token"] is None
    assert new_order(api, email="not an address").status_code == 400
    assert new_order(api, email="a@b.com\nBcc: c@d.com").status_code == 400


def test_a_mail_failure_does_not_fail_the_order(api, mail, monkeypatch):
    def down(msg):
        raise OSError("smtp down")
    monkeypatch.setattr(notify, "_send", down)
    created = new_order(api, email="buyer@example.com").json()
    order = finish(created["id"])
    assert order["status"] == "done" and order["notified_at"] is None and order["notify_token"]
    monkeypatch.setattr(notify, "_send", mail.append)
    notify.send_pending()                      # the hourly retry
    assert len(mail) == 1 and db.get(created["id"])["notify_token"] is None


def test_files_are_deleted_after_the_download_period(api, mail, store):
    created = new_order(api, email="buyer@example.com").json()
    oid, token = created["id"], created["buyer_token"]
    order = finish(oid)
    assert (store / "jobs" / oid / "manga.pdf").exists()

    worker._expire_results(now=order["done_at"] + 7 * 86400 - 60)
    assert (store / "jobs" / oid).exists()
    worker._expire_results(now=order["done_at"] + 7 * 86400 + 60)
    assert not (store / "jobs" / oid).exists()
    order = db.get(oid)
    assert order["files_deleted_at"] and order["email"] is None

    status = api.get(f"/api/orders/{oid}", headers={"x-buyer-token": token}).json()
    assert status["files_expired"] is True and "downloads" not in status
    assert api.get(f"/api/orders/{oid}/manga.pdf?t={token}").status_code == 410
