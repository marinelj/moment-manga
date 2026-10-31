import unittest
import base64
from unittest.mock import patch

from alipay.aop.api.util.SignatureUtils import get_sign_content
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.alipay_web import AlipayWeb, amount_text


class FormRequest:
    def __init__(self, fields):
        self.fields = fields

    async def form(self):
        return self.fields


class AlipayWebSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = AlipayWeb.__new__(AlipayWeb)
        self.provider.app_id = "app-1"
        self.provider.seller_id = "seller-1"
        self.provider.verify_signature = lambda fields: fields.get("sign") == "valid"
        self.order = {"id": "order-1", "provider": "alipay_web", "app_id": "app-1",
                      "seller_id": "seller-1", "amount_yuan": "39.00"}
        self.fields = {"sign": "valid", "app_id": "app-1", "seller_id": "seller-1",
                       "out_trade_no": "order-1", "trade_no": "trade-1",
                       "notify_id": "notify-1", "total_amount": "39.00",
                       "trade_status": "TRADE_SUCCESS"}

    def test_amount_rejects_fractional_cents(self):
        self.assertEqual(amount_text("39"), "39.00")
        with self.assertRaises(ValueError):
            amount_text("39.001")

    async def test_notification_rejects_changed_amount_or_seller(self):
        with patch("app.alipay_web.db.get", return_value=self.order), \
             patch("app.alipay_web.db.record_alipay_notification") as record:
            for change in ({"total_amount": "39.01"}, {"seller_id": "other"},
                           {"sign": "invalid"}):
                self.assertEqual(await self.provider.notify(FormRequest(self.fields | change)), "fail")
            record.assert_not_called()

    async def test_refund_notification_never_marks_paid(self):
        with patch("app.alipay_web.db.get", return_value=self.order), \
             patch("app.alipay_web.db.record_alipay_notification") as record:
            self.assertEqual(await self.provider.notify(FormRequest(self.fields | {"refund_fee": "39.00"})),
                             "success")
            self.assertFalse(record.call_args.args[-1])

    def test_query_requires_matching_order_amount_and_paid_state(self):
        result = {"out_trade_no": "order-1", "trade_no": "trade-1",
                  "trade_status": "TRADE_SUCCESS", "total_amount": "39.00"}
        self.assertTrue(self.provider.verify_trade(self.order, result))
        self.assertFalse(self.provider.verify_trade(self.order, result | {"total_amount": "39.01"}))
        self.assertFalse(self.provider.verify_trade(self.order, result | {"trade_status": "WAIT_BUYER_PAY"}))

    async def test_real_rsa2_signature_is_required_for_notification(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.provider.public_key = "".join(pem.splitlines()[1:-1])
        del self.provider.verify_signature
        fields = self.fields | {"sign_type": "RSA2"}
        signed = get_sign_content({k: v for k, v in fields.items() if k not in ("sign", "sign_type")})
        fields["sign"] = base64.b64encode(key.sign(signed.encode(), padding.PKCS1v15(),
                                                   hashes.SHA256())).decode()
        with patch("app.alipay_web.db.get", return_value=self.order), \
             patch("app.alipay_web.db.record_alipay_notification") as record:
            self.assertEqual(await self.provider.notify(FormRequest(fields)), "success")
            record.assert_called_once()
            self.assertEqual(await self.provider.notify(FormRequest(fields | {"total_amount": "39.01"})),
                             "fail")
