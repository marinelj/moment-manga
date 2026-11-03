"""Admin debug page: switch mocked/real Gemini at runtime; real-money payment test flag."""
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, main, modes, settings


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
def admin(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for (sql,) in db._conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        conn.execute(sql)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(settings, "ALIPAY_ENV", "sandbox")
    monkeypatch.setattr(settings, "GENERATION_MODE", "sample")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(settings, "env", lambda name, default=None: "tok" if name == "ALIPAY_ADMIN_TOKEN" else default)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(main, "provider", FakeAlipay())
    monkeypatch.setattr(main, "validate_video", lambda path: 10.0)
    yield TestClient(main.app)
    conn.close()


AUTH = {"authorization": "Bearer tok"}


def test_page_loads_and_actions_need_the_token(admin):
    assert "Moment Manga admin" in admin.get("/admin").text
    assert admin.get("/api/admin/status").status_code == 403
    assert admin.post("/api/admin/generation", json={"mode": "live"}).status_code == 403
    assert modes.current() == "sample"


def test_switch_applies_to_new_orders_only(admin):
    first = admin.post("/api/orders", files={"video": ("a.mp4", b"v", "video/mp4")}, data={"product": "book", "lang": "en"}).json()
    assert admin.post("/api/admin/generation", json={"mode": "live"}, headers=AUTH).json()["mode"] == "live"
    assert admin.get("/api/config").json()["generation"] == "live"
    second = admin.post("/api/orders", files={"video": ("a.mp4", b"v", "video/mp4")}, data={"product": "book", "lang": "en"}).json()
    assert db.get(first["id"])["generation"] == "sample" and db.get(second["id"])["generation"] == "live"

    status = admin.get("/api/admin/status", headers=AUTH).json()
    assert status["mode"] == "live" and status["mode_source"] == "admin page"
    assert {o["id"] for o in status["orders"]} == {first["id"], second["id"]}

    assert admin.post("/api/admin/generation", json={"mode": None}, headers=AUTH).json()["mode"] == "sample"
    assert admin.post("/api/admin/generation", json={"mode": "free"}, headers=AUTH).status_code == 400


def test_real_money_with_sample_files_needs_the_explicit_flag(admin, monkeypatch):
    monkeypatch.setattr(settings, "ALIPAY_ENV", "production")
    assert main.checkout_ready() is False
    monkeypatch.setattr(settings, "ALLOW_PRODUCTION_PAYMENT_TEST", True)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    assert main.checkout_ready() is True              # no Gemini key, no Gemini cost
    modes.set_override("live")
    assert main.checkout_ready() is False             # real generation needs the key
