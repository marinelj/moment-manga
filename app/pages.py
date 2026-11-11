"""Slanted manga pages with lettering, for the directed book (app/directed.py).

A page is a stack of rows. A row holds one or two cells, and a cell holds one panel or two stacked panels. The
lines between rows and between stacked panels are slanted, so every panel is a four-cornered shape with upright
sides: the look of an action manga page instead of a grid of rectangles.

Captions sit in a corner of their panel in a white box; sound effects are big outlined letters at a point the
director chose on the drawing; a "text" panel is a black panel with a few big words.
"""
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import settings

PAGE_W, PAGE_H, MARGIN, GUTTER, SLANT = 1654, 2339, 60, 25, 34
HEIGHTS = {"tall": 1.55, "normal": 1.0, "short": 0.62}
SPLITS = {"even": 0.5, "left-wide": 0.63, "right-wide": 0.37}
SFX_SIZES = {"small": 110, "medium": 150, "large": 200}           # letter height in a panel 1000 px wide
SFX_COLOURS = {"white": "white", "yellow": "#ffe14a", "red": "#ff4a3d"}
FONTS = Path(__file__).resolve().parent / "assets" / "fonts"
LATIN = {"sfx": FONTS / "Bangers-Regular.ttf", "caption": FONTS / "ComicNeue-Bold.ttf"}
# Chinese captions and sound effects: FONT_PATH first, then fonts that ship with macOS and common Linux packages.
CJK = ["/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
       "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
       "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]


# ---------- shapes ----------

def page_shapes(rows: list[dict], flip: bool = False) -> list[list[tuple[int, int]]]:
    """The panel shapes of one page, in reading order: rows top to bottom, cells left to right, a stack top to
    bottom. Each row is {"height": tall|normal|short, "split": even|left-wide|right-wide, "cells": [1 or 2, ...]},
    where each cell is the number of panels stacked in it. Each shape is [top-left, top-right, bottom-right,
    bottom-left]. flip mirrors the slants, so facing pages don't lean the same way."""
    x0, x1, top, bottom = MARGIN, PAGE_W - MARGIN, MARGIN, PAGE_H - MARGIN
    weights = [HEIGHTS.get(r.get("height"), 1.0) for r in rows]
    edges, y = [float(top)], float(top)
    for w in weights:
        y += (bottom - top) * w / sum(weights)
        edges.append(y)
    sign = -1 if flip else 1

    def edge(k: int, x: float) -> float:
        """Height of the line under row k-1 at x. The page's top and bottom are level."""
        if k in (0, len(rows)):
            return edges[k]
        lean = SLANT * sign * (1 if k % 2 else -1)
        return edges[k] + lean - 2 * lean * (x - x0) / (x1 - x0)

    def side(k: int, x: float, upper: bool) -> float:
        gap = 0 if k in (0, len(rows)) else GUTTER / 2
        return edge(k, x) + (gap if upper else -gap)

    shapes = []
    for r, row in enumerate(rows):
        cells = [max(1, min(2, int(n))) for n in (row.get("cells") or [1])][:2]
        if len(cells) == 2:
            cut = x0 + (x1 - x0) * SPLITS.get(row.get("split"), 0.5)
            spans = [(x0, cut - GUTTER / 2), (cut + GUTTER / 2, x1)]
        else:
            spans = [(x0, x1)]
        for c, ((a, b), stacked) in enumerate(zip(spans, cells)):
            ta, tb, ba, bb = side(r, a, True), side(r, b, True), side(r + 1, a, False), side(r + 1, b, False)
            if stacked == 1:
                shapes.append(_shape(a, b, ta, tb, ba, bb))
                continue
            lean = SLANT * 0.6 * sign * (1 if (r + c) % 2 else -1)
            ma, mb = (ta + ba) / 2 + lean, (tb + bb) / 2 - lean
            shapes.append(_shape(a, b, ta, tb, ma - GUTTER / 2, mb - GUTTER / 2))
            shapes.append(_shape(a, b, ma + GUTTER / 2, mb + GUTTER / 2, ba, bb))
    return shapes


def _shape(a, b, ta, tb, ba, bb) -> list[tuple[int, int]]:
    return [(round(a), round(ta)), (round(b), round(tb)), (round(b), round(bb)), (round(a), round(ba))]


def bounds(shape) -> tuple[int, int, int, int]:
    xs, ys = [p[0] for p in shape], [p[1] for p in shape]
    return min(xs), min(ys), max(xs), max(ys)


def ratio(shape) -> float:
    x0, y0, x1, y1 = bounds(shape)
    return (x1 - x0) / max(1, y1 - y0)


# ---------- fonts ----------

def _is_cjk(text: str) -> bool:
    return any(ord(ch) >= 0x2E80 for ch in text)


@lru_cache(maxsize=256)
def _load(path: str, size: int):
    return ImageFont.truetype(path, size)


def font(kind: str, size: int, text: str = ""):
    paths = ([settings.FONT_PATH] + CJK) if _is_cjk(text) else [str(LATIN[kind]), settings.FONT_PATH] + CJK
    for path in paths:
        try:
            return _load(str(path), int(size))
        except OSError:
            continue
    return ImageFont.load_default(int(size))


# ---------- drawing ----------

def _cover(img: Image.Image, w: int, h: int, focus: tuple[float, float]) -> tuple[Image.Image, float, int, int]:
    """img scaled to cover w x h and cropped so the focus point (0-1 of the image) is as near the middle as the
    image allows. Returns the tile, the scale and the crop's left and top."""
    scale = max(w / img.width, h / img.height)
    big = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))), Image.LANCZOS)
    left = round(min(max(0, focus[0] * big.width - w / 2), big.width - w))
    top = round(min(max(0, focus[1] * big.height - h / 2), big.height - h))
    return big.crop((left, top, left + w, top + h)), scale, left, top


def _paste(page: Image.Image, tile: Image.Image, shape) -> None:
    x0, y0, x1, y1 = bounds(shape)
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in shape], fill=255)
    page.paste(tile, (x0, y0), mask)
    ImageDraw.Draw(page).line(list(shape) + [shape[0]], fill="black", width=8, joint="curve")


def _wrap(draw, text: str, fnt, max_width: float) -> list[str]:
    """Lines no wider than max_width: by words, or by characters for text without spaces (Chinese)."""
    lines = []
    for para in str(text).split("\n"):
        units = para.split(" ") if " " in para else list(para)
        glue = " " if " " in para else ""
        cur = ""
        for unit in units:
            trial = (cur + glue + unit) if cur else unit
            if cur and draw.textlength(trial, font=fnt) > max_width:
                lines.append(cur)
                cur = unit
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines or [""]


def caption(page: Image.Image, shape, text: str, corner: str = "tl") -> None:
    """A caption box in one corner of the panel (tl, tr, bl, br), kept inside the slanted edge."""
    x0, y0, x1, y1 = bounds(shape)
    width = x1 - x0
    size = 40 if width >= 900 else 36 if width >= 600 else 34
    fnt, draw = font("caption", size, text), ImageDraw.Draw(page)
    pad, inset = 16, 22
    lines = _wrap(draw, text, fnt, min(660, width * 0.66) - 2 * pad)
    line_h = int(size * 1.25)
    bw = int(max(draw.textlength(line, font=fnt) for line in lines) + 2 * pad)
    bh = line_h * len(lines) + 2 * pad - 6
    x = x0 + inset if "l" in corner else x1 - inset - bw
    (tl, tr, br, bl) = shape

    def along(p, q, at):          # height of the edge p-q at x = at
        return p[1] + (q[1] - p[1]) * (at - p[0]) / max(1, q[0] - p[0])

    if "t" in corner:
        y = int(max(along(tl, tr, x), along(tl, tr, x + bw)) + inset)
    else:
        y = int(min(along(bl, br, x), along(bl, br, x + bw)) - inset - bh)
    draw.rectangle((x + 7, y + 7, x + bw + 7, y + bh + 7), fill="black")
    draw.rectangle((x, y, x + bw, y + bh), fill="white", outline="black", width=4)
    for i, line in enumerate(lines):
        draw.text((x + pad, y + pad - 6 + i * line_h), line, font=fnt, fill="black")


def sfx(page: Image.Image, shape, text: str, at: tuple[float, float], size: str = "medium", angle: float = 0,
        colour: str = "white") -> None:
    """Big outlined letters centred on a page point, turned by angle, pulled back inside the panel if they stick out."""
    x0, y0, x1, y1 = bounds(shape)
    px = int(SFX_SIZES.get(size, SFX_SIZES["medium"]) * min(1.1, max(0.75, (x1 - x0) / 1000)))
    px = max(60, min(px, int((y1 - y0) * 0.4)))
    fnt = font("sfx", px, text)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    while px > 50 and probe.textlength(text, font=fnt) > (x1 - x0) * 0.8:      # a long word in a narrow panel
        px = int(px * 0.9)
        fnt = font("sfx", px, text)
    stroke = max(8, px // 11)
    box = probe.textbbox((0, 0), text, font=fnt, stroke_width=stroke)
    art = Image.new("RGBA", (box[2] - box[0] + 40, box[3] - box[1] + 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)
    draw.text((28 - box[0], 28 - box[1]), text, font=fnt, fill="black", stroke_width=stroke, stroke_fill="black")
    draw.text((20 - box[0], 20 - box[1]), text, font=fnt, fill=SFX_COLOURS.get(colour, "white"),
              stroke_width=stroke, stroke_fill="black")
    art = art.rotate(max(-20, min(20, angle)), expand=True, resample=Image.BICUBIC)
    left = int(min(max(x0 + 10, at[0] - art.width / 2), max(x0 + 10, x1 - 10 - art.width)))
    top = int(min(max(y0 + 10, at[1] - art.height / 2), max(y0 + 10, y1 - 10 - art.height)))
    page.paste(art, (left, top), art)


def text_panel(page: Image.Image, shape, words: str) -> None:
    """A black panel with a few big white words, for a beat between two drawings."""
    x0, y0, x1, y1 = bounds(shape)
    _paste(page, Image.new("RGB", (x1 - x0, y1 - y0), "black"), shape)
    draw = ImageDraw.Draw(page)
    lines = [w for w in str(words).replace("\n", " ").split(" ") if w] or [""]
    if len(lines) > 3 or _is_cjk(words):          # more than three words: let them wrap instead of one a line
        lines = None
    size = 230
    while size > 40:
        fnt = font("sfx", size, words)
        fit = lines or _wrap(draw, words, fnt, (x1 - x0) * 0.8)
        line_h = int(size * 1.05)
        if (max(draw.textlength(line, font=fnt) for line in fit) <= (x1 - x0) * 0.8
                and line_h * len(fit) <= (y1 - y0) * 0.7):
            break
        size = int(size * 0.9)
    y = (y0 + y1) // 2 - line_h * len(fit) // 2
    for line in fit:
        draw.text(((x0 + x1) // 2 - draw.textlength(line, font=fnt) // 2, y), line, font=fnt, fill="white")
        y += line_h


def build_page(items: list[dict], number: int | None = None) -> Image.Image:
    """One page. Each item: {"shape": [...], "kind": "art" | "text", "art": path (art), "words": str (text),
    "focus": [x, y] 0-1 of the drawing, "caption": str, "corner": tl|tr|bl|br, "sfx": str, "sfx_at": [x, y] 0-1 of
    the drawing, "sfx_size", "sfx_angle", "sfx_colour"}. Panels first, then all lettering on top."""
    page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    letters = []
    for item in items:
        shape = [tuple(p) for p in item["shape"]]
        if item.get("kind") == "text":
            text_panel(page, shape, item.get("words") or "")
            continue
        x0, y0, x1, y1 = bounds(shape)
        with Image.open(item["art"]) as img:
            art = img.convert("RGB")
        focus = item.get("focus") or (0.5, 0.5)
        tile, scale, left, top = _cover(art, x1 - x0, y1 - y0, focus)
        _paste(page, tile, shape)
        if item.get("caption"):
            letters.append(lambda s=shape, i=item: caption(page, s, i["caption"], i.get("corner") or "tl"))
        if item.get("sfx"):
            u, v = item.get("sfx_at") or (0.5, 0.8)
            at = (x0 + u * art.width * scale - left, y0 + v * art.height * scale - top)
            letters.append(lambda s=shape, i=item, at=at: sfx(page, s, i["sfx"], at, i.get("sfx_size") or "medium",
                                                              i.get("sfx_angle") or 0, i.get("sfx_colour") or "white"))
    for draw in letters:
        draw()
    if number:
        fnt = font("caption", 26, str(number))
        ImageDraw.Draw(page).text((PAGE_W // 2 - 8, PAGE_H - 46), str(number), font=fnt, fill="black")
    return page
