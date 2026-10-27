"""Payment providers. Pick one with PAYMENT_PROVIDER.

Each provider turns an order into a URL/string that the page renders as a QR code,
and verifies the provider's server-to-server notification before an order is marked paid.
Credentials are read from the environment only.
"""
import json
from decimal import Decimal

from fastapi import Request

from . import settings
from .settings import env

AMOUNT_YUAN = str((Decimal(settings.PRICE_CENTS) / 100).quantize(Decimal("0.01")))
SUBJECT = "Moment Manga · 漫画定制"


class Demo:
    """For local testing: the QR opens /api/dev/pay/<id>, which marks the order paid."""
    methods = ["wechat", "alipay"]

    def create(self, order):
        return f"{settings.PUBLIC_URL}/api/dev/pay/{order['id']}", None

    async def notify(self, request: Request):
        return None, "ok"


class WeChatNative:
    """WeChat Pay v3 'Native' (扫码支付): returns a weixin:// code_url to show as a QR.
    Needs a WeChat Pay merchant account (商户号) tied to a business licence."""
    methods = ["wechat"]

    def __init__(self):
        from wechatpayv3 import WeChatPay, WeChatPayType
        self.client = WeChatPay(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=env("WECHAT_MCHID"),
            private_key=open(env("WECHAT_PRIVATE_KEY_PATH")).read(),
            cert_serial_no=env("WECHAT_CERT_SERIAL_NO"),
            apiv3_key=env("WECHAT_APIV3_KEY"),
            appid=env("WECHAT_APPID"),
            notify_url=f"{settings.PUBLIC_URL}/api/pay/notify",
            cert_dir=str(settings.DATA_DIR / "wechat_certs"),
        )

    def create(self, order):
        code, msg = self.client.pay(description=SUBJECT, out_trade_no=order["id"],
                                    amount={"total": settings.PRICE_CENTS, "currency": "CNY"})
        data = json.loads(msg)
        if code != 200:
            raise RuntimeError(f"WeChat Pay error: {data}")
        return data["code_url"], order["id"]

    async def notify(self, request: Request):
        body = await request.body()
        result = self.client.callback(request.headers, body)
        ok = {"code": "SUCCESS", "message": "成功"}
        if not result or result.get("event_type") != "TRANSACTION.SUCCESS":
            return None, ok
        res = result["resource"]
        if res.get("trade_state") == "SUCCESS" and res["amount"]["total"] == settings.PRICE_CENTS:
            return res["out_trade_no"], ok
        return None, ok


class AlipayPrecreate:
    """Alipay 当面付 precreate: returns a qr_code URL. Needs an Alipay open-platform app."""
    methods = ["alipay"]

    def __init__(self):
        from alipay import AliPay
        self.client = AliPay(
            appid=env("ALIPAY_APPID"),
            app_notify_url=f"{settings.PUBLIC_URL}/api/pay/notify",
            app_private_key_string=open(env("ALIPAY_PRIVATE_KEY_PATH")).read(),
            alipay_public_key_string=open(env("ALIPAY_PUBLIC_KEY_PATH")).read(),
            sign_type="RSA2",
        )

    def create(self, order):
        r = self.client.api_alipay_trade_precreate(subject=SUBJECT, out_trade_no=order["id"],
                                                   total_amount=AMOUNT_YUAN)
        if r.get("code") != "10000":
            raise RuntimeError(f"Alipay error: {r}")
        return r["qr_code"], order["id"]

    async def notify(self, request: Request):
        data = dict(await request.form())
        sign = data.pop("sign", None)
        if (sign and self.client.verify(data, sign)
                and data.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED")
                and data.get("total_amount") == AMOUNT_YUAN):
            return data["out_trade_no"], "success"
        return None, "success"


class StripeCheckout:
    """Stripe Checkout with cards, Alipay and WeChat Pay. The QR opens the hosted
    checkout page on the buyer's phone. Needs a Stripe account outside mainland China
    (e.g. Hong Kong, Singapore, US)."""
    methods = ["wechat", "alipay", "card"]

    def __init__(self):
        import stripe
        stripe.api_key = env("STRIPE_SECRET_KEY")
        self.stripe = stripe

    def create(self, order):
        s = self.stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "alipay", "wechat_pay"],
            payment_method_options={"wechat_pay": {"client": "web"}},
            line_items=[{"quantity": 1, "price_data": {
                "currency": settings.CURRENCY, "unit_amount": settings.PRICE_CENTS,
                "product_data": {"name": SUBJECT}}}],
            metadata={"order_id": order["id"]},
            success_url=f"{settings.PUBLIC_URL}/?paid=1",
            cancel_url=f"{settings.PUBLIC_URL}/",
        )
        return s.url, s.id

    async def notify(self, request: Request):
        event = self.stripe.Webhook.construct_event(
            await request.body(), request.headers.get("stripe-signature"), env("STRIPE_WEBHOOK_SECRET"))
        if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            s = event["data"]["object"]
            if s["payment_status"] == "paid" and s["amount_total"] == settings.PRICE_CENTS:
                return s["metadata"]["order_id"], {"received": True}
        return None, {"received": True}


def load():
    return {"demo": Demo, "wechat": WeChatNative, "alipay": AlipayPrecreate,
            "stripe": StripeCheckout}[settings.PAYMENT_PROVIDER]()
