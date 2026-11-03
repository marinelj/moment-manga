"""The generation mode in force right now: the admin page's choice, else GENERATION_MODE."""
from . import db, settings

KEY = "generation_mode"


def current() -> str:
    return db.get_setting(KEY) or settings.GENERATION_MODE


def set_override(mode: str | None) -> None:
    if mode is not None and mode not in ("live", "sample"):
        raise ValueError("mode must be live or sample")
    db.set_setting(KEY, mode)
