import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, main, settings


class FakeAlipay:
    methods = ["alipay"]
    prices = {"book": "5.00"}
    app_id = "test-app"
    seller_id = "test-seller"

    def payment(self, order):
        return {"pay_url": f"/api/orders/{order['id']}/pay",
                "charge": {"amount": order["amount_yuan"], "currency": "CNY"}}

    def page_form(self, order):
        return "<form>test checkout</form>"

    def query(self, order):
        return {"code": "40004", "trade_status": "WAIT_BUYER_PAY"}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for (sql,) in db._conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
        conn.execute(sql)
    monkeypatch.setattr(db, "_conn", conn)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(main, "provider", FakeAlipay())
    monkeypatch.setattr(main, "checkout_ready", lambda: True)
    monkeypatch.setattr(main, "validate_video", lambda path: None)
    yield TestClient(main.app), tmp_path
    conn.close()


def test_checkout_requires_buyer_cookie_and_shows_exact_charge(checkout):
    client, tmp_path = checkout
    config = client.get("/api/config").json()
    assert config["checkout_ready"] is True
    assert config["products"] == ["book"]
    assert config["charges"]["book"] == {"amount": "5.00", "currency": "CNY"}

    response = client.post("/api/orders", files={"video": ("play.mp4", b"video", "video/mp4")},
                           data={"product": "book", "lang": "en"})
    assert response.status_code == 200
    order = response.json()
    assert order["payment"]["charge"] == config["charges"]["book"]
    assert client.get(f"/api/orders/{order['id']}").status_code == 200
    assert client.get(order["payment"]["pay_url"]).status_code == 200

    stranger = TestClient(main.app)
    assert stranger.get(f"/api/orders/{order['id']}").status_code == 403
    assert stranger.get(order["payment"]["pay_url"]).status_code == 403
    pdf = tmp_path / "manga.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    db.update(order["id"], status="done", result=str(pdf))
    assert stranger.get(f"/api/orders/{order['id']}/manga.pdf").status_code == 403
    assert client.get(f"/api/orders/{order['id']}/manga.pdf").status_code == 200


def test_invalid_photo_does_not_create_order_or_leave_uploads(checkout):
    client, tmp_path = checkout
    response = client.post("/api/orders", files={
        "video": ("play.mp4", b"video", "video/mp4"),
        "photo": ("hero.jpg", b"invalid image", "image/jpeg"),
    }, data={"product": "book", "lang": "en"})
    assert response.status_code == 400
    assert db._conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert list((tmp_path / "uploads").iterdir()) == []


def test_checkout_is_closed_without_generation_dependencies(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay_web")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(main, "provider", FakeAlipay())
    client = TestClient(main.app)
    assert client.get("/api/config").json()["checkout_ready"] is False
    response = client.post("/api/orders", files={"video": ("play.mp4", b"video", "video/mp4")},
                           data={"product": "book", "lang": "en"})
    assert response.status_code == 503


def test_video_probe_rejects_corrupt_and_long_clips(tmp_path):
    video = tmp_path / "play.mp4"
    video.write_bytes(b"video")
    with patch("app.main.subprocess.run") as run:
        run.return_value.returncode = 1
        with pytest.raises(main.HTTPException) as error:
            main.validate_video(video)
        assert error.value.status_code == 400
        run.return_value.returncode = 0
        run.return_value.stdout = '{"format":{"duration":"121"},"streams":[{"codec_type":"video"}]}'
        with pytest.raises(main.HTTPException) as error:
            main.validate_video(video)
        assert error.value.status_code == 400


def test_partial_refunds_cannot_reserve_more_than_paid_amount(checkout):
    order = db.create("sports", "slamdunk", "")
    db.update(order["id"], provider="alipay_web", amount_yuan="5.00", status="paid", paid_at=1)
    assert db.reserve_refund(order["id"], "first", "3.00") is True
    assert db.reserve_refund(order["id"], "first", "3.00") is False
    with pytest.raises(ValueError, match="exceeds"):
        db.reserve_refund(order["id"], "second", "3.00")
    assert db.get_refund("second") is None


def test_closed_unpaid_order_cannot_be_refunded(checkout):
    order = db.create("sports", "slamdunk", "")
    db.update(order["id"], provider="alipay_web", amount_yuan="5.00", status="failed")
    with pytest.raises(ValueError, match="Only paid"):
        db.reserve_refund(order["id"], "closed-order", "1.00")
