"""Runtime switches from the admin page. Each falls back to its environment setting.

generation: live (real Gemini) or sample (sample files, no model calls).
api style: standard (interactive Gemini calls) or batch (Gemini Batch API for the book panels, half price, slower).
book director: who studies the clip and checks the drawings of a real book (auto, claude, grok or off).
"""
from . import db, settings

KEY = "generation_mode"
STYLE_KEY = "gemini_api_style"
API_STYLES = ("standard", "batch")
DIRECTOR_KEY = "book_director"
DIRECTORS = ("auto", "claude", "grok", "off")


def current() -> str:
    return db.get_setting(KEY) or settings.GENERATION_MODE


def set_override(mode: str | None) -> None:
    if mode is not None and mode not in ("live", "sample"):
        raise ValueError("mode must be live or sample")
    db.set_setting(KEY, mode)


def api_style() -> str:
    style = db.get_setting(STYLE_KEY) or settings.GEMINI_API_STYLE
    return style if style in API_STYLES else "standard"


def set_api_style(style: str | None) -> None:
    if style is not None and style not in API_STYLES:
        raise ValueError("style must be standard or batch")
    db.set_setting(STYLE_KEY, style)


def book_director() -> str:
    choice = db.get_setting(DIRECTOR_KEY) or settings.BOOK_DIRECTOR
    return choice if choice in DIRECTORS else "auto"


def set_book_director(choice: str | None) -> None:
    if choice is not None and choice not in DIRECTORS:
        raise ValueError("director must be auto, claude, grok or off")
    db.set_setting(DIRECTOR_KEY, choice)
