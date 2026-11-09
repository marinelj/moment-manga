"""Optional "your manga is ready" email with the order link. Off until SMTP_HOST is set.

The link carries the buyer token, which the database otherwise keeps only as a hash. For an order whose
buyer left an email address, the token waits in notify_token until the email is sent, then it is erased.
The address itself is erased with the files, DOWNLOAD_DAYS after the order is done.
"""
import smtplib
import ssl
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlencode

from . import db, settings

CHINA = timezone(timedelta(hours=8))     # buyers pay with Alipay; dates in the email use Beijing time


def enabled() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def order_link(order: dict, token: str) -> str:
    page = settings.ORDER_PAGE_URL + ("zh/" if order.get("lang") == "zh" else "")
    return page + "?" + urlencode({"order": order["id"], "t": token})


def expires_at(order: dict) -> float | None:
    return order["done_at"] + settings.DOWNLOAD_DAYS * 86400 if order.get("done_at") else None


def message(order: dict, link: str) -> EmailMessage:
    days, test = settings.DOWNLOAD_DAYS, order.get("generation") == "sample"
    until = datetime.fromtimestamp(expires_at(order) or time.time(), CHINA).strftime("%Y-%m-%d")
    msg = EmailMessage()
    if order.get("lang") == "zh":
        msg["Subject"] = ("[测试] " if test else "") + "你的 Moment Manga 漫画已完成"
        body = (f"你的漫画已经画好了。\n\n打开订单页面下载：\n{link}\n\n"
                f"文件会在完成 {days} 天后删除（{until}），请在此之前下载。\n"
                "这个链接可以直接打开你的订单，请不要转发。\n\nMoment Manga\n")
    else:
        msg["Subject"] = ("[Test] " if test else "") + "Your Moment Manga is ready"
        body = (f"Your manga is ready.\n\nOpen your order to download it:\n{link}\n\n"
                f"The files are deleted {days} days after they are ready, on {until}. Download them before then.\n"
                "This link opens your order, so please don't share it.\n\nMoment Manga\n")
    msg["From"] = formataddr(("Moment Manga", settings.SMTP_FROM))
    msg["To"] = order["email"]
    msg.set_content(body)
    return msg


def _send(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.SMTP_STARTTLS:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        server.starttls(context=context)
    else:
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30, context=context)
    with server:
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


def order_ready(oid: str) -> bool:
    """Send the ready email once. Never raises: a mail problem must not fail a finished order."""
    try:
        order = db.get(oid)
        if not (enabled() and order and order["status"] == "done" and order.get("email")
                and order.get("notify_token") and not order.get("files_deleted_at")):
            return False
        if not db.claim_notice(oid):
            return False
        try:
            _send(message(order, order_link(order, order["notify_token"])))
        except Exception:
            db.update(oid, notified_at=None)      # the hourly retry tries again for a day
            raise
        db.update(oid, notify_token=None)
        return True
    except Exception:
        traceback.print_exc()
        return False


def send_pending() -> None:
    for oid in db.unsent_ready_notices(time.time() - 86400):
        order_ready(oid)
