"""Video ("all") product: pipeline with a fake Gemini client, cost ledger, cache, budget, API and worker."""
import base64
import io
import json
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, main, settings, video, worker

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg is not installed")


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for (sql,) in db._conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        conn.execute(sql)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    yield tmp_path
    conn.close()


def make_clip(path, seconds, size="640x360"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
                    "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


class FakeGemini:
    """Answers like the real SDK objects the pipeline reads."""

    def __init__(self, clip_bytes, flag_first=True, fix_ruins=False):
        self.calls = {"image": 0, "json": 0, "motion": 0}
        self.flag_first, self.clip, self.fix_ruins = flag_first, clip_bytes, fix_ruins
        self.models = SimpleNamespace(generate_content=self._generate)
        self.files = SimpleNamespace(upload=lambda file: SimpleNamespace(state="ACTIVE", uri="files/x", name="x"),
                                     get=lambda name: SimpleNamespace(state="ACTIVE", uri="files/x", name=name))
        self.interactions = SimpleNamespace(create=self._create, get=self._get)

    def _generate(self, model, contents, config):
        if config.response_modalities == ["IMAGE"]:
            self.calls["image"] += 1
            buf = io.BytesIO()
            Image.new("RGB", (1280, 720), (self.calls["image"] * 7 % 255,) * 3).save(buf, "PNG")
            part = SimpleNamespace(inline_data=SimpleNamespace(data=buf.getvalue()))
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        self.calls["json"] += 1
        prompt = contents[-1]
        if prompt == video.INSPECT_PROMPT:
            data = {"overlays": [[900, 0, 1000, 300]], "teams": ["royal blue", "white"]}
        else:
            bad = self.flag_first and self.calls["json"] == 2      # first keyframe check
            ruined = self.fix_ruins and self.calls["json"] == 3    # the check of that fix
            data = {"text": bad, "extra_people": False, "colours_ok": True, "manga": not ruined,
                    "fix": "remove the banner text"}
        usage = SimpleNamespace(prompt_token_count=1000, candidates_token_count=50)
        return SimpleNamespace(text=json.dumps(data), usage_metadata=usage)

    def _create(self, **kwargs):
        self.calls["motion"] += 1
        assert kwargs["background"] is True
        return SimpleNamespace(id=f"job{self.calls['motion']}", status="in_progress")

    def _get(self, job_id):
        data = {"status": "completed", "usage": {"total_output_tokens": 38000, "total_thought_tokens": 500},
                "output_video": {"type": "video", "data": base64.b64encode(self.clip).decode()}}
        return SimpleNamespace(id=job_id, status="completed", model_dump=lambda: data)


def test_pieces_cover_the_clip_with_overlap():
    assert video.pieces(5.0) == ([0.0], 5.0)
    starts, length = video.pieces(12.1)
    assert len(starts) == 2 and length <= video.PIECE_MAX
    assert starts[-1] + length == pytest.approx(12.1)
    starts, length = video.pieces(15.0)
    assert len(starts) == 3 and length <= video.PIECE_MAX
    assert starts[-1] + length == pytest.approx(15.0)


def test_team_rule_names_both_teams():
    assert "royal blue" in video.team_rule(["royal blue", "white"])
    assert "black-and-white" in video.team_rule([])


@needs_ffmpeg
def test_video_run_makes_mp4_records_costs_and_reuses_cache(store):
    clip = make_clip(store / "play.mp4", 8)
    motion_clip = make_clip(store / "motion.mp4", 4.2, "1280x720").read_bytes()
    fake = FakeGemini(motion_clip)
    order = db.create("sports", "slamdunk", str(clip))
    db.update(order["id"], product="all")
    seen = []

    out = video.run(db.get(order["id"]), progress=seen.append, client=fake, sleep=lambda s: None)

    duration, w, h = video.probe(out)
    assert (w, h) == (1280, 720) and duration == pytest.approx(8, abs=0.2)
    assert seen[-1] == 100
    assert fake.calls == {"image": 17, "json": 18, "motion": 2}    # 16 keyframes + 1 fix, re-checked
    lines = db.order_costs(order["id"])
    keyframes = [line for line in lines if line["step"] == "keyframe"]
    assert len(keyframes) == 16 and all(line["usd"] == video.IMAGE_PRICE["1K"] for line in keyframes)
    assert len([line for line in lines if line["step"] == "fix"]) == 1
    motion = [line for line in lines if line["step"] == "motion"]
    assert motion[0]["usd"] == pytest.approx(38500 * video.MOTION_PRICE_OUT)
    first_total = db.order_cost_total(order["id"])
    assert first_total == pytest.approx(17 * 0.067 + 2 * 38500 * 17.5e-6 + 18 * (1000 * 0.1e-6 + 50 * 0.4e-6))

    # The same clip again (for example a retried order) is served from the cache at no cost.
    again = db.create("sports", "slamdunk", str(clip))
    fake.calls = {"image": 0, "json": 0, "motion": 0}
    video.run(db.get(again["id"]), client=fake, sleep=lambda s: None)
    assert fake.calls == {"image": 0, "json": 0, "motion": 0}
    assert db.order_cost_total(again["id"]) == 0
    assert all(line["cached"] for line in db.order_costs(again["id"]))


@needs_ffmpeg
def test_budget_stops_the_order_before_overspending(store, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_ORDER_BUDGET_USD", 0.30)
    clip = make_clip(store / "play.mp4", 8)
    fake = FakeGemini(b"", flag_first=False)
    order = db.create("sports", "slamdunk", str(clip))
    with pytest.raises(video.BudgetExceeded):
        video.run(db.get(order["id"]), client=fake, sleep=lambda s: None)
    assert db.order_cost_total(order["id"]) <= 0.30
    assert fake.calls["motion"] == 0


def test_rate_limit_is_retried_then_raised():
    attempts, waits = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"

    assert video._retry(flaky, waits.append) == "ok" and waits == [30, 60]
    with pytest.raises(ValueError):
        video._retry(lambda: (_ for _ in ()).throw(ValueError("400 bad prompt")), waits.append)


class FakeAlipay:
    methods = ["alipay"]
    prices = {"book": "5.00", "all": "10.00"}
    app_id = "test-app"
    seller_id = "test-seller"

    def payment(self, order):
        return {"pay_url": f"/api/orders/{order['id']}/pay",
                "charge": {"amount": order["amount_yuan"], "currency": "CNY"}}

    def query(self, order):
        return {"code": "40004", "trade_status": "WAIT_BUYER_PAY"}


@pytest.fixture
def checkout(store, monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(main, "provider", FakeAlipay())
    monkeypatch.setattr(main, "checkout_ready", lambda: True)
    monkeypatch.setattr(main, "validate_video", lambda path: 12.0)
    return TestClient(main.app)


def test_all_package_order_status_and_video_download(checkout, store, monkeypatch):
    config = checkout.get("/api/config").json()
    assert config["products"] == ["book", "all"] and config["video_max_seconds"] == settings.VIDEO_MAX_SECONDS
    response = checkout.post("/api/orders", files={"video": ("play.mp4", b"video", "video/mp4")},
                             data={"product": "all", "lang": "en"})
    assert response.status_code == 200
    oid = response.json()["id"]
    assert db.get(oid)["product"] == "all" and db.get(oid)["amount_yuan"] == "10.00"

    (store / "manga.pdf").write_bytes(b"%PDF-1.4\n")
    (store / "manga.mp4").write_bytes(b"mp4")
    db.update(oid, status="done", result=str(store / "manga.pdf"), video_result=str(store / "manga.mp4"))
    status = checkout.get(f"/api/orders/{oid}").json()
    assert status["downloads"] == {"book": f"/api/orders/{oid}/manga.pdf", "clip": f"/api/orders/{oid}/manga.mp4"}
    assert checkout.get(status["downloads"]["clip"]).status_code == 200
    assert TestClient(main.app).get(status["downloads"]["clip"]).status_code == 403

    monkeypatch.setattr(settings, "env", lambda name, default=None: "secret" if name == "ADMIN_TOKEN" else default)
    db.add_cost(oid, "keyframe", "m", 1, 0.067)
    costs = checkout.get(f"/api/admin/orders/{oid}/costs", headers={"authorization": "Bearer secret"}).json()
    assert costs["total_usd"] == 0.067 and costs["lines"][0]["step"] == "keyframe"
    assert checkout.get(f"/api/admin/orders/{oid}/costs").status_code == 200   # debug routes are open


def test_all_package_refuses_long_clips(checkout, monkeypatch):
    monkeypatch.setattr(main, "validate_video", lambda path: settings.VIDEO_MAX_SECONDS + 1)
    response = checkout.post("/api/orders", files={"video": ("play.mp4", b"video", "video/mp4")},
                             data={"product": "all", "lang": "en"})
    assert response.status_code == 400
    assert db._conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    book = checkout.post("/api/orders", files={"video": ("play.mp4", b"video", "video/mp4")},
                         data={"product": "book", "lang": "en"})
    assert book.status_code == 200


def test_worker_makes_book_then_video_and_keeps_book_if_video_fails(store, monkeypatch):
    monkeypatch.setattr(worker.pipeline, "run", lambda order, progress: store / "manga.pdf")
    monkeypatch.setattr(worker.video, "run", lambda order, progress: store / "manga.mp4")
    order = db.create("sports", "slamdunk", "clip.mp4")
    db.update(order["id"], product="all", status="processing")
    worker.process(db.get(order["id"]))
    done = db.get(order["id"])
    assert done["status"] == "done" and done["result"].endswith("manga.pdf") and done["video_result"].endswith("manga.mp4")

    def broken(order, progress):
        raise video.BudgetExceeded("over budget")
    monkeypatch.setattr(worker.video, "run", broken)
    failed = db.create("sports", "slamdunk", "clip.mp4")
    db.update(failed["id"], product="all", status="processing")
    worker.process(db.get(failed["id"]))
    row = db.get(failed["id"])
    assert row["status"] == "failed" and row["result"].endswith("manga.pdf") and "over budget" in row["error"]

    book = db.create("sports", "slamdunk", "clip.mp4")
    db.update(book["id"], product="book", status="processing")
    worker.process(db.get(book["id"]))
    assert db.get(book["id"])["video_result"] is None


@needs_ffmpeg
def test_a_fix_that_stops_looking_like_manga_is_thrown_away(store):
    clip = make_clip(store / "play.mp4", 1.0)
    fake = FakeGemini(b"", fix_ruins=True)
    order = db.create("sports", "slamdunk", str(clip))
    frames = video.sample_frames(clip, store / "src", 1.0, 0.5, (1280, 720))
    cache, ledger = video.Cache(store / "cache"), video.Ledger(order["id"], 4.0)
    fake.calls["json"] = 1                                    # as if inspect already ran
    keyframes = video.draw_keyframes(fake, ledger, cache, frames, settings.VIDEO_STYLE_REF, "rule", "16:9",
                                     store / "kf", lambda s: None, lambda i, n: None)
    first_drawing = Image.open(keyframes[0]).getpixel((0, 0))
    assert first_drawing == (7, 7, 7)                         # the original drawing, not the fix (14)
    assert video.problems({"manga": False}) > video.problems({"text": True, "extra_people": True})
