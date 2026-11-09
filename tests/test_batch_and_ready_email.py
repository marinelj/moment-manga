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

    FACTS = {"overlays": [[900, 0, 1000, 1000]], "age": "teenagers",
             "groups": [{"name": "", "look": "black jerseys, white numbers"}],
             "lead": {"look": "black jersey number 5", "why": "shoots"},
             "events": [{"t": 0.4 * i, "what": "w", "box": [300, 300, 700, 700], "certain": True} for i in range(5)],
             "ending": "the shot goes in"}
    PLAN = {"title": "t", "cast": {"age": "teenagers", "lead": "black jersey number 5", "groups": ["black", "navy"]},
            "pages": [
        {"panels": [{"event": i, "t": 0.4 * i, "box": [200, 200, 800, 800], "shot": "wide", "description": "d",
                     "caption": "c", "sfx": ""} for i in range(3)]},
        {"panels": [{"event": i, "t": 0.4 * i, "box": [400, 400, 600, 600], "shot": "close-up", "description": "d",
                     "caption": "", "sfx": "!"} for i in range(4)]}]}
    GOOD = {"text": False, "extra_people": False, "flipped": False, "colours_ok": True, "lead_ok": True,
            "style_ok": True, "fix": ""}

    def __init__(self, states=("JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"), missing=1, errors=1, verdicts=()):
        self.states, self.missing, self.errors = list(states), missing, errors
        self.verdicts = list(verdicts)          # answers for the first panel checks; the rest are GOOD
        self.storyboards = self.images = self.created = self.checks = 0
        self.cancelled, self.requests, self.calls = [], [], []
        self.models = SimpleNamespace(generate_content=self._generate)
        self.files = SimpleNamespace(upload=self._upload, download=self._download)
        self.batches = SimpleNamespace(create=self._create, get=self._get,
                                       cancel=lambda name: self.cancelled.append(name))

    def _generate(self, model, contents, config):
        self.calls.append((model, contents))
        if config.response_modalities == ["IMAGE"]:
            self.images += 1
            part = SimpleNamespace(inline_data=SimpleNamespace(data=_png(200)))
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        if model == settings.VIDEO_CHECK_MODEL:
            self.checks += 1
            usage = SimpleNamespace(prompt_token_count=3_000, candidates_token_count=100, thoughts_token_count=0)
            verdict = self.verdicts.pop(0) if self.verdicts else self.GOOD
            return SimpleNamespace(text=json.dumps(verdict), usage_metadata=usage)
        usage = SimpleNamespace(prompt_token_count=26_000, candidates_token_count=1_000, thoughts_token_count=1_000)
        if str(contents[0]).startswith("These"):
            self.storyboards += 1
            return SimpleNamespace(text=json.dumps(self.FACTS), usage_metadata=usage)
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
    """Panel lines only; the other steps are checked in their own tests."""
    return sorted((line["step"], line["usd"]) for line in db.order_costs("order1")
                  if line["step"].startswith("book panel"))


def steps():
    return [line["step"] for line in db.order_costs("order1")]


@needs_ffmpeg
def test_batch_book_costs_half_and_draws_what_batch_missed_on_standard(store, clip):
    fake = FakeGemini(missing=1, errors=1)
    pdf = pipeline.run(book_order(clip, "batch"), client=fake, sleep=lambda s: None, now=lambda: 0)
    assert pdf.exists() and fake.created == 1
    assert len(fake.requests) == 7
    request = fake.requests[0]["request"]
    assert request["generationConfig"] == {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3"}}
    parts = request["contents"][0]["parts"]
    # The panel's own source, the lead's character sheet and the style reference, then the prompt.
    assert [p["inlineData"]["mimeType"] for p in parts[:3]] == ["image/jpeg"] * 3 and "text" in parts[3]
    assert fake.images == 1 + 2                 # the sheet; one request failed in the job, one never came back
    assert costs() == [("book panel", 0.140)] * 2 + [("book panel (batch)", 0.070)] * 5
    story = [line for line in db.order_costs("order1") if line["step"].startswith("storyboard")]
    assert [line["step"] for line in story] == ["storyboard (facts)", "storyboard (panels)"]
    assert story[0]["usd"] == pytest.approx(26_000 * 0.75e-6 + 2_000 * 3.75e-6)
    assert steps().count("character sheet") == 1 and steps().count("panel check") == 7


@needs_ffmpeg
def test_restarted_batch_order_waits_on_the_same_job(store, clip):
    fake = FakeGemini(states=("JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"), missing=0, errors=0)

    def server_stops(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(book_order(clip, "batch"), client=fake, sleep=server_stops, now=lambda: 0)
    assert (store / "jobs" / "order1" / "batch.json").exists() and costs() == []

    pipeline.run(book_order(clip, "batch"), client=fake, sleep=lambda s: None, now=lambda: 0)
    assert fake.created == 1 and fake.storyboards == 1 and fake.images == 1    # the sheet, drawn once
    assert costs() == [("book panel (batch)", 0.070)] * 7


@needs_ffmpeg
def test_batch_past_the_deadline_is_cancelled_and_drawn_on_standard(store, clip):
    fake = FakeGemini(states=("JOB_STATE_RUNNING",))
    clock = [0.0]
    pipeline.run(book_order(clip, "batch"), client=fake,
                 sleep=lambda s: clock.__setitem__(0, clock[0] + 30 * 3600), now=lambda: clock[0])
    assert fake.cancelled == ["batches/1"] and fake.images == 1 + 7
    assert costs() == [("book panel", 0.140)] * 7


@needs_ffmpeg
def test_standard_book_reuses_panels_sheet_and_checks_already_done(store, clip):
    fake = FakeGemini()
    pipeline.run(book_order(clip, "standard"), client=fake)
    pipeline.run(book_order(clip, "standard"), client=fake)
    assert fake.created == 0 and fake.storyboards == 1 and fake.images == 1 + 7 and fake.checks == 7
    assert costs() == [("book panel", 0.140)] * 7


@needs_ffmpeg
def test_flagged_panels_get_one_fix_each_up_to_the_cap(store, clip, monkeypatch):
    monkeypatch.setattr(settings, "BOOK_MAX_FIXES", 2)
    bad_text = dict(FakeGemini.GOOD, text=True, fix="remove the wall lettering")
    mirrored = dict(FakeGemini.GOOD, flipped=True)
    wrong_kit = dict(FakeGemini.GOOD, colours_ok=False)
    # P1-1 has text, P1-2 is mirrored, P1-3 has the wrong kit; then the re-checks of the two fixes say GOOD.
    fake = FakeGemini(verdicts=[bad_text, mirrored, wrong_kit])
    pipeline.run(book_order(clip, "standard"), client=fake)
    fixes = [line["step"] for line in db.order_costs("order1") if line["step"] in ("panel fix", "panel redraw")]
    assert sorted(fixes) == ["panel fix", "panel redraw"]       # the cap leaves the third alone
    assert fake.checks == 7 + 2
    log = json.loads((store / "jobs" / "order1" / "checks.json").read_text())
    assert log["P1-1"]["fixed"] is True and log["P1-2"]["fixed"] is True and "fixed" not in log["P1-3"]
    edit = [c for m, c in fake.calls if m == settings.GEMINI_IMAGE_MODEL and "Edit the first image" in str(c[-1])]
    assert len(edit) == 1 and "remove the wall lettering" in edit[0][-1]


@needs_ffmpeg
def test_a_fix_that_is_no_better_is_thrown_away(store, clip):
    bad = dict(FakeGemini.GOOD, text=True)
    worse = dict(FakeGemini.GOOD, text=True, style_ok=False)
    fake = FakeGemini(verdicts=[bad, FakeGemini.GOOD, FakeGemini.GOOD, FakeGemini.GOOD, FakeGemini.GOOD,
                                FakeGemini.GOOD, FakeGemini.GOOD, worse])
    pipeline.run(book_order(clip, "standard"), client=fake)
    log = json.loads((store / "jobs" / "order1" / "checks.json").read_text())
    assert log["P1-1"]["fixed"] is False
    assert not (store / "jobs" / "order1" / "P1-1.fix.png").exists()


@needs_ffmpeg
def test_storyboard_sees_dense_frames_then_zooms_and_never_the_score_bar(store, clip):
    fake = FakeGemini()
    pipeline.run(book_order(clip, "standard"), client=fake)
    facts_call, plan_call = [c for m, c in fake.calls if m == settings.GEMINI_TEXT_MODEL]
    frames = [x for x in facts_call if getattr(x, "inline_data", None)]
    assert len(frames) == 12                      # 3 s at 4 frames a second
    assert {x.inline_data.mime_type for x in frames} == {"image/jpeg"}
    assert "Never report a foul" in facts_call[0]
    # The second pass watches the same 12 moments again, zoomed on the play.
    assert sum(isinstance(x, str) and x.startswith("Zoom frame ") for x in plan_call) == 12
    assert "never by adding events" in plan_call[0] and "for EVERY frame, note who has the ball" in plan_call[0]
    assert "\"w\"" not in plan_call[0]          # the first pass's events (who did what) are not passed on
    # Panel sources are full-resolution crops with the score bar (bottom 10%) painted out.
    src = Image.open(store / "jobs" / "order1" / "src" / "P1-1.jpg")
    assert src.width >= 1024


def test_panel_prompt_locks_composition_cast_age_and_sheet():
    panel = {"shot": "close-up", "description": "black #5 pulls up for a jump shot over navy #22"}
    cast = {"age": "teenagers", "lead": "black jersey number 5", "groups": ["black jerseys", "navy jerseys"]}
    prompt = pipeline.panel_prompt(panel, "slamdunk", "3:4", cast, sheet=True, technique=True)
    for words in ("ONLY source of content", "NOT flipped", "No extra or missing people", "teenagers",
                  "black jerseys; navy jerseys", "Image 2 is the character sheet", "Other players keep their own looks", "Image 3 is a small crop",
                  "black-and-white ink", "except numbers on clothes", "pulls up for a jump shot"):
        assert words in prompt


def test_crop_keeps_the_box_at_the_panel_shape():
    img = Image.new("RGB", (1280, 720))
    out = pipeline.crop(img, [400, 450, 600, 550], 3 / 4, min_frac=0.2)
    assert abs(out.width / out.height - 0.75) < 0.01 and out.width >= 256
    whole = pipeline.crop(img, None, 16 / 9)
    assert whole.size == (1280, 720)
    assert pipeline.nearest_aspect(1514 / 1085) == "4:3" and pipeline.nearest_aspect(743 / 1085) == "2:3"


def test_follow_box_tracks_the_play_and_panel_boxes_map_back_to_the_full_frame():
    events = [{"t": 0, "box": [400, 0, 600, 100]}, {"t": 2, "box": [400, 900, 600, 1000]}]
    left0 = pipeline.follow_box(events, 0, (1280, 720))[0]
    left1 = pipeline.follow_box(events, 1, (1280, 720))[0]
    left2 = pipeline.follow_box(events, 2, (1280, 720))[0]
    assert left0 == 0 and left0 < left1 < left2
    rect = (320, 180, 960, 540)                               # the middle quarter of the frame
    assert pipeline._zoom_to_full([0, 0, 1000, 1000], rect, (1280, 720)) == [250.0, 250.0, 750.0, 750.0]
    assert pipeline._zoom_to_full(None, rect, (1280, 720)) == [250.0, 250.0, 750.0, 750.0]


def test_old_plans_with_frame_numbers_still_work():
    plan = {"pages": [{"panels": [{"frame": 6, "shot": "wide", "description": "d"}] * 5}]}
    out = pipeline.normalise_plan(plan, {}, 12.0)
    assert len(out["pages"][0]["panels"]) == 4 and out["pages"][0]["panels"][0]["t"] == 3.0


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
