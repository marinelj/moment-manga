"""GENERATION_MODE=sample: payments can be tested without any model calls."""
import shutil
import sqlite3

import pytest

from app import db, main, pipeline, settings, video, worker


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


def test_sandbox_defaults_to_sample_and_explicit_choice_wins():
    assert settings.generation_mode(None, "alipay_web", "sandbox") == "sample"
    assert settings.generation_mode("", "alipay_web", "production") == "live"
    assert settings.generation_mode("LIVE", "alipay_web", "sandbox") == "live"
    assert settings.generation_mode(None, "demo", "sandbox") == "live"


def test_sample_order_makes_no_model_calls(store, monkeypatch):
    def paid(*args, **kwargs):
        raise AssertionError("a paid pipeline ran in sample mode")
    monkeypatch.setattr(settings, "GENERATION_MODE", "sample")
    monkeypatch.setattr(pipeline, "run", paid)
    monkeypatch.setattr(video, "run", paid)
    order = db.create("sports", "slamdunk", str(store / "missing.mp4"))
    db.update(order["id"], product="all", status="processing")

    worker.process(db.get(order["id"]))

    done = db.get(order["id"])
    assert done["status"] == "done" and done["progress"] == 100
    assert open(done["result"], "rb").read(5) == b"%PDF-"
    assert done["video_result"].endswith("manga.mp4")
    assert db.order_cost_total(order["id"]) == 0

    book = db.create("sports", "slamdunk", "")
    db.update(book["id"], product="book", status="processing")
    worker.process(db.get(book["id"]))
    assert db.get(book["id"])["status"] == "done" and db.get(book["id"])["video_result"] is None


def test_checkout_readiness_by_mode(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(settings, "ALIPAY_ENV", "sandbox")
    monkeypatch.setattr(settings, "GENERATION_MODE", "sample")
    assert main.checkout_ready() is True               # no Gemini key needed for samples
    monkeypatch.setattr(settings, "GENERATION_MODE", "live")
    assert main.checkout_ready() is False              # live generation needs the key
    monkeypatch.setattr(settings, "ALIPAY_ENV", "production")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(settings, "GENERATION_MODE", "sample")
    assert main.checkout_ready() is False              # never sell sample files for real money
    monkeypatch.setattr(settings, "GENERATION_MODE", "typo")
    assert main.checkout_ready() is False


def test_a_test_order_stays_a_test_order_after_switching_to_live(store, monkeypatch):
    def paid(*args, **kwargs):
        raise AssertionError("a paid pipeline ran for a payment-test order")
    monkeypatch.setattr(pipeline, "run", paid)
    monkeypatch.setattr(video, "run", paid)
    order = db.create("sports", "slamdunk", "")
    db.update(order["id"], product="all", status="processing", generation="sample")
    monkeypatch.setattr(settings, "GENERATION_MODE", "live")     # restarted with live generation
    worker.process(db.get(order["id"]))
    assert db.get(order["id"])["status"] == "done"


def test_status_marks_payment_test_orders(store, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "demo")
    order = db.create("sports", "slamdunk", "")
    db.update(order["id"], generation="sample", status="processing")
    body = TestClient(main.app).get(f"/api/orders/{order['id']}").json()
    assert body["payment_test"] is True
    live = db.create("sports", "slamdunk", "")
    db.update(live["id"], generation="live", status="processing")
    assert "payment_test" not in TestClient(main.app).get(f"/api/orders/{live['id']}").json()
