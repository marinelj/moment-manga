"""All configuration comes from environment variables. Never hard-code keys."""
import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


DATA_DIR = Path(env("DATA_DIR", "./data")).resolve()
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000").rstrip("/")

PRICE_CENTS = int(env("PRICE_CENTS", "3900"))          # ¥39.00
CURRENCY = env("CURRENCY", "cny")
PRICE_LABEL = env("PRICE_LABEL", "¥39")
MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "300"))

# demo | wechat | alipay | stripe
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "demo")

GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_TEXT_MODEL = env("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL = env("GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
FONT_PATH = env("FONT_PATH", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

DATA_DIR.mkdir(parents=True, exist_ok=True)
