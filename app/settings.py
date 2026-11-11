"""All configuration comes from environment variables. Never hard-code keys."""
import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


DATA_DIR = Path(env("DATA_DIR", "./data")).resolve()
# Pages on other origins (e.g. https://marinelj.github.io) that may call this API. Comma-separated.
CORS_ORIGINS = [o.strip().rstrip("/") for o in (env("CORS_ORIGINS") or "").split(",") if o.strip()]
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000").rstrip("/")

PRICE_CENTS = int(env("PRICE_CENTS", "3900"))          # ¥39.00
CURRENCY = env("CURRENCY", "cny")
PRICE_LABEL = env("PRICE_LABEL", "¥39")
MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "300"))

# demo | wechat | alipay | stripe
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "demo")

GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_TEXT_MODEL = env("GEMINI_TEXT_MODEL", "gemini-3.8-flash")
GEMINI_IMAGE_MODEL = env("GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
# Book storyboard: frames per second sampled for the facts pass (fast moves last about 0.3 s), and a cap.
STORY_FPS = float(env("STORY_FPS", "4"))
STORY_MAX_FRAMES = int(env("STORY_MAX_FRAMES", "60"))
# Book check-and-fix: at most this many panels per book get one fix (an image edit or a redraw, $0.14 each).
BOOK_MAX_FIXES = int(env("BOOK_MAX_FIXES", "4"))
# The directed book (app/directed.py): Claude studies the frames, directs every panel and reviews every drawing;
# Gemini draws. It runs for every real book order while ANTHROPIC_API_KEY is set, unless BOOK_DIRECTOR=off;
# without a key, orders get the Gemini-only book above.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
BOOK_DIRECTOR = (env("BOOK_DIRECTOR") or "auto").strip().lower()             # auto | off
DIRECTOR_MODEL = env("DIRECTOR_MODEL", "claude-fable-5-2")
DIRECTOR_EFFORT = env("DIRECTOR_EFFORT", "high")                              # low | medium | high | xhigh | max
DIRECTOR_LOCATE_EFFORT = env("DIRECTOR_LOCATE_EFFORT", "medium")              # the first pass only finds the play
DIRECTOR_MAX_TOKENS = int(env("DIRECTOR_MAX_TOKENS", "64000"))                # per answer, thinking included
# USD per million tokens for a director model that app/director.py has no list price for.
DIRECTOR_PRICE_IN = float(env("DIRECTOR_PRICE_IN", "8"))
DIRECTOR_PRICE_OUT = float(env("DIRECTOR_PRICE_OUT", "40"))
DIRECTOR_LOCATE_FPS = float(env("DIRECTOR_LOCATE_FPS", "2"))                  # whole frames for the first pass
DIRECTOR_ZOOM_FPS = float(env("DIRECTOR_ZOOM_FPS", "10"))                     # zoom frames; fast moves last 0.3 s
DIRECTOR_MAX_FRAMES = int(env("DIRECTOR_MAX_FRAMES", "100"))                  # longer clips get fewer a second
DIRECTOR_ZOOM_FRAC = float(env("DIRECTOR_ZOOM_FRAC", "0.34"))                 # a zoom frame is at least this wide
DIRECTOR_ZOOM_PX = int(env("DIRECTOR_ZOOM_PX", "1152"))                       # and is sent at most this wide
DIRECTOR_FIX_ROUNDS = int(env("DIRECTOR_FIX_ROUNDS", "2"))                    # review, fix, review again
DIRECTOR_MAX_FIXES = int(env("DIRECTOR_MAX_FIXES", "10"))                     # per book, $0.14 each
BOOK_IMAGE_SIZE = env("BOOK_IMAGE_SIZE", "2K")                                # directed panels; 1K costs the same
# Moving-manga video ("all" product): Nano Banana keyframes, Omni motion, cheap QA model
VIDEO_IMAGE_MODEL = env("VIDEO_IMAGE_MODEL", "gemini-3.1-flash-image")
VIDEO_CHECK_MODEL = env("VIDEO_CHECK_MODEL", "gemini-3.5-flash-lite")
VIDEO_MOTION_MODEL = env("VIDEO_MOTION_MODEL", "gemini-omni-1.1-flash")
VIDEO_KF_SIZE = env("VIDEO_KF_SIZE", "1K")                      # 1K | 512
VIDEO_KF_INTERVAL = float(env("VIDEO_KF_INTERVAL", "0.5"))      # seconds between keyframes
VIDEO_MAX_SECONDS = float(env("VIDEO_MAX_SECONDS", "15"))       # longer clips are refused for "all"
VIDEO_ORDER_BUDGET_USD = float(env("VIDEO_ORDER_BUDGET_USD", "4.00"))  # hard stop per order
VIDEO_STYLE_REF = Path(env("VIDEO_STYLE_REF", str(Path(__file__).resolve().parent / "assets" / "style_slamdunk.png")))
# How the book panels reach Gemini. standard: interactive calls, a book in about 5 minutes.
# batch: one Gemini Batch API job per order at half the panel price; Google aims for 24 hours, and many jobs
# finish much sooner. The /admin page can override it for new orders; each order keeps the style it was made with.
GEMINI_API_STYLE = (env("GEMINI_API_STYLE") or "standard").strip().lower()
BATCH_MAX_WAIT_HOURS = float(env("BATCH_MAX_WAIT_HOURS", "24"))   # then cancel and draw the rest on standard
BATCH_POLL_SECONDS = float(env("BATCH_POLL_SECONDS", "60"))
# live: paid orders run the real (paid) Gemini pipelines.
# sample: paid orders get sample files instead, so payments can be tested without model costs.
# Default is sample whenever Alipay runs against its sandbox.
ALIPAY_ENV = env("ALIPAY_ENV", "sandbox")


def generation_mode(explicit: str | None, provider: str, alipay_env: str) -> str:
    chosen = (explicit or "").strip().lower()
    if chosen:
        return chosen
    return "sample" if provider == "alipay_web" and alipay_env == "sandbox" else "live"


GENERATION_MODE = generation_mode(env("GENERATION_MODE"), PAYMENT_PROVIDER, ALIPAY_ENV)
# Real-money payment test: allow sample mode with ALIPAY_ENV=production (sample files, no model costs).
# Off unless set to 1, so customers are never charged for sample files by accident.
ALLOW_PRODUCTION_PAYMENT_TEST = env("ALLOW_PRODUCTION_PAYMENT_TEST", "") == "1"
# Finished files are deleted this many days after the order is done. The page and the ready email say so.
DOWNLOAD_DAYS = int(env("DOWNLOAD_DAYS", "7"))
# Customer page for the order link in the ready email; the Chinese page is <ORDER_PAGE_URL>zh/.
ORDER_PAGE_URL = (env("ORDER_PAGE_URL") or "https://marinelj.github.io/moment-manga/").rstrip("/") + "/"
# Optional "your manga is ready" email to buyers who leave an address. Off while SMTP_HOST is empty.
# Port 465 uses SSL; set SMTP_STARTTLS=1 for port 587.
SMTP_HOST = env("SMTP_HOST", "")
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER", "")
SMTP_PASSWORD = env("SMTP_PASSWORD", "")
SMTP_FROM = env("SMTP_FROM", "") or SMTP_USER
SMTP_STARTTLS = env("SMTP_STARTTLS", "") == "1"
FONT_PATH = env("FONT_PATH", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

DATA_DIR.mkdir(parents=True, exist_ok=True)
