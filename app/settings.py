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
# Moving-manga video ("all" product): Nano Banana keyframes, Omni motion, cheap QA model
VIDEO_IMAGE_MODEL = env("VIDEO_IMAGE_MODEL", "gemini-3.1-flash-image")
VIDEO_CHECK_MODEL = env("VIDEO_CHECK_MODEL", "gemini-3.5-flash-lite")
VIDEO_MOTION_MODEL = env("VIDEO_MOTION_MODEL", "gemini-omni-1.1-flash")
VIDEO_KF_SIZE = env("VIDEO_KF_SIZE", "1K")                      # 1K | 512
VIDEO_KF_INTERVAL = float(env("VIDEO_KF_INTERVAL", "0.5"))      # seconds between keyframes
VIDEO_MAX_SECONDS = float(env("VIDEO_MAX_SECONDS", "15"))       # longer clips are refused for "all"
VIDEO_ORDER_BUDGET_USD = float(env("VIDEO_ORDER_BUDGET_USD", "4.00"))  # hard stop per order
VIDEO_STYLE_REF = Path(env("VIDEO_STYLE_REF", str(Path(__file__).resolve().parent / "assets" / "style_slamdunk.png")))
FONT_PATH = env("FONT_PATH", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

DATA_DIR.mkdir(parents=True, exist_ok=True)
