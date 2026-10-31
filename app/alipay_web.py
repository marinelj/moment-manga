"""Alipay AI web application payment (page pay), using the official Python SDK."""
import json
import time
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from . import db, settings

SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
PRODUCTION_GATEWAY = "https://openapi.alipay.com/gateway.do"


def amount_text(value: str) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount") from exc
    if not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("100000000"):
        raise ValueError("Amount outside Alipay range")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Amount has more than two decimal places")
    return f"{amount:.2f}"


class AlipayWeb:
    methods = ["alipay"]

    def __init__(self):
        from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
        from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

        mode = settings.env("ALIPAY_ENV", "sandbox")
        if mode == "sandbox":
            # Resolve from this module, independent of the process working directory.
            path = Path(__file__).resolve().parent.parent / ".alipay-sandbox.json"
            if path.stat().st_mode & 0o077:
                raise ValueError("Sandbox configuration must be owner-only")
            data = json.loads(path.read_text(encoding="utf-8"))
            apps = data.get("appIds", [])
            if len(apps) != 1 or apps[0].get("type") != "PUBLICAPP":
                raise ValueError("Expected one verified public application in sandbox config")
            app = apps[0]
            self.app_id = app["appId"]
            self.seller_id = app["pid"]
            private_key = app["appPrivatePkcsKey"]
            public_key = app["alipayPublicKey"]
            gateway = SANDBOX_GATEWAY
        elif mode == "production":
            self.app_id = settings.env("ALIPAY_APP_ID")
            self.seller_id = settings.env("ALIPAY_SELLER_ID")
            private_key = settings.env("ALIPAY_APP_PRIVATE_KEY")
            public_key = settings.env("ALIPAY_PUBLIC_KEY")
            gateway = PRODUCTION_GATEWAY
            notify = settings.env("ALIPAY_NOTIFY_URL", "")
            if (not settings.PUBLIC_URL.startswith("https://")
                    or urlparse(notify).scheme != "https" or not urlparse(notify).netloc):
                raise ValueError("Production requires HTTPS public and notification URLs")
        else:
            raise ValueError("ALIPAY_ENV must be sandbox or production")
        if not all((self.app_id, self.seller_id, private_key, public_key)):
            raise ValueError("Alipay application configuration is incomplete")
        if "-----BEGIN" in private_key or "-----BEGIN" in public_key:
            raise ValueError("Alipay SDK requires raw key strings")
        self.mode = mode
        self.public_key = public_key
        config = AlipayClientConfig()
        config.app_id = self.app_id
        config.app_private_key = private_key
        config.alipay_public_key = public_key
        config.server_url = gateway
        config.charset = "utf-8"
        config.sign_type = "RSA2"
        self.client = DefaultAlipayClient(alipay_client_config=config)
        self.prices = {"book": self._price("ALIPAY_BOOK_CENTS")}

    @staticmethod
    def _price(name: str) -> str:
        raw = settings.env(name)
        if not raw or not raw.isdecimal() or int(raw) <= 0:
            raise ValueError(f"{name} must be an explicitly approved positive CNY amount")
        return amount_text(Decimal(int(raw)) / 100)

    @staticmethod
    def _response(raw: str, method: str) -> dict:
        data = json.loads(raw)
        result = data.get(method + "_response")
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected Alipay response")
        return result

    def payment(self, order: dict) -> dict:
        if (order.get("provider") != "alipay_web" or order.get("app_id") != self.app_id
                or order.get("seller_id") != self.seller_id):
            raise ValueError("Order does not belong to this Alipay application")
        return {"pay_url": f"/api/orders/{order['id']}/pay",
                "charge": {"amount": order["amount_yuan"], "currency": "CNY"}}

    def page_form(self, order: dict) -> str:
        from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
        from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
        if (order["status"] != "unpaid" or order["app_id"] != self.app_id
                or order["seller_id"] != self.seller_id):
            raise ValueError("Order is not available for payment")
        request = AlipayTradePagePayRequest()
        model = AlipayTradePagePayModel()
        model.out_trade_no = order["id"]
        model.total_amount = amount_text(order["amount_yuan"])
        model.subject = "Moment Manga - manga book"
        model.product_code = "FAST_INSTANT_TRADE_PAY"
        model.timeout_express = "30m"
        request.biz_model = model
        request.return_url = f"{settings.PUBLIC_URL}/api/pay/return"
        notify_url = settings.env("ALIPAY_NOTIFY_URL", "").strip()
        if notify_url:
            url = urlparse(notify_url)
            if url.scheme != "https" or not url.netloc:
                raise ValueError("ALIPAY_NOTIFY_URL must be public HTTPS")
            request.notify_url = notify_url
        return self.client.page_execute(request, http_method="POST")

    def verify_signature(self, params: dict) -> bool:
        from alipay.aop.api.util.SignatureUtils import get_sign_content, verify_with_rsa
        sign = params.get("sign")
        if not sign or params.get("sign_type") != "RSA2":
            return False
        signed = {key: value for key, value in params.items() if key not in ("sign", "sign_type")}
        try:
            message = get_sign_content(signed).encode("utf-8")
            return verify_with_rsa(self.public_key, message, sign)
        except Exception:
            return False

    def verify_trade(self, order: dict, result: dict) -> bool:
        try:
            return (order.get("app_id") == self.app_id
                    and order.get("seller_id") == self.seller_id
                    and result.get("seller_id", self.seller_id) == self.seller_id
                    and result.get("out_trade_no") == order["id"]
                    and result.get("trade_no")
                    and result.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED")
                    and amount_text(result.get("total_amount")) == order["amount_yuan"])
        except ValueError:
            return False

    def query(self, order: dict) -> dict:
        from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
        from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
        request = AlipayTradeQueryRequest()
        model = AlipayTradeQueryModel()
        model.out_trade_no = order["id"]
        request.biz_model = model
        result = self._response(self.client.execute(request), "alipay_trade_query")
        if result.get("code") == "10000" and self.verify_trade(order, result):
            db.mark_alipay_paid_from_query(order["id"], result["trade_no"])
        return result

    def refund(self, order: dict, request_no: str, amount: str) -> dict:
        from alipay.aop.api.request.AlipayTradeRefundRequest import AlipayTradeRefundRequest
        from alipay.aop.api.domain.AlipayTradeRefundModel import AlipayTradeRefundModel
        amount = amount_text(amount)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_no):
            raise ValueError("Invalid refund request number")
        if order["status"] not in ("paid", "processing", "done", "failed") or not order.get("paid_at"):
            raise ValueError("Only paid orders can be refunded")
        db.reserve_refund(order["id"], request_no, amount)
        request = AlipayTradeRefundRequest()
        model = AlipayTradeRefundModel()
        model.out_trade_no = order["id"]
        model.refund_amount = amount
        model.out_request_no = request_no
        request.biz_model = model
        result = self._response(self.client.execute(request), "alipay_trade_refund")
        if (result.get("code") == "10000" and result.get("fund_change") == "Y"
                and result.get("out_trade_no") == order["id"]):
            db.set_refund_status(request_no, "succeeded")
        return result

    def refund_query(self, order: dict, request_no: str) -> dict:
        from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import AlipayTradeFastpayRefundQueryRequest
        from alipay.aop.api.domain.AlipayTradeFastpayRefundQueryModel import AlipayTradeFastpayRefundQueryModel
        saved = db.get_refund(request_no)
        if not saved or saved["out_trade_no"] != order["id"]:
            raise ValueError("Unknown refund request")
        if time.time() - saved["created"] < 10:
            raise ValueError("Wait at least 10 seconds before querying a refund")
        request = AlipayTradeFastpayRefundQueryRequest()
        model = AlipayTradeFastpayRefundQueryModel()
        model.out_trade_no = order["id"]
        model.out_request_no = request_no
        request.biz_model = model
        result = self._response(self.client.execute(request), "alipay_trade_fastpay_refund_query")
        if (result.get("code") == "10000" and result.get("refund_status") == "REFUND_SUCCESS"
                and result.get("out_trade_no") == order["id"]
                and result.get("out_request_no") == request_no):
            db.set_refund_status(request_no, "succeeded")
        return result

    def close(self, order: dict) -> dict:
        from alipay.aop.api.request.AlipayTradeCloseRequest import AlipayTradeCloseRequest
        from alipay.aop.api.domain.AlipayTradeCloseModel import AlipayTradeCloseModel
        if order["status"] != "unpaid":
            raise ValueError("Only unpaid orders can be closed")
        request = AlipayTradeCloseRequest()
        model = AlipayTradeCloseModel()
        model.out_trade_no = order["id"]
        request.biz_model = model
        result = self._response(self.client.execute(request), "alipay_trade_close")
        if result.get("code") == "10000" and result.get("out_trade_no") == order["id"]:
            db.mark_alipay_closed(order["id"])
        return result

    async def notify(self, request) -> str:
        try:
            params = dict(await request.form())
            if params.get("app_id") != self.app_id or not self.verify_signature(params):
                return "fail"
            order = db.get(params.get("out_trade_no", ""))
            if not order or order.get("provider") != "alipay_web":
                return "fail"
            if (order.get("app_id") != params.get("app_id")
                    or order.get("seller_id") != params.get("seller_id")
                    or params.get("seller_id") != self.seller_id
                    or amount_text(params.get("total_amount")) != order.get("amount_yuan")
                    or not params.get("notify_id") or not params.get("trade_no")):
                return "fail"
            event_type = params.get("trade_status", "")
            paid = (event_type in ("TRADE_SUCCESS", "TRADE_FINISHED")
                    and not any(params.get(k) for k in ("out_biz_no", "gmt_refund", "refund_fee")))
            db.record_alipay_notification(order["id"], params["notify_id"],
                                          params["trade_no"], event_type, paid)
            return "success"
        except Exception:
            return "fail"
