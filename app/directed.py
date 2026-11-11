"""The directed book: Claude directs (app/director.py), Gemini draws, slanted pages (app/pages.py).

1. Locate. Whole frames, a few a second, go to the director: where the play is over time, the on-screen
   graphics, the basket.
2. Study and plan. Up to DIRECTOR_MAX_FRAMES zoom frames, ten a second, follow the play. The director writes a
   note per frame, the facts, the cast, and the pages: for every panel a re-staged shot (close-up, low angle...)
   whose events are exactly the clip's, with the zoom frame and box to use as a reference.
3. Character sheet. A strip of the clearest crops of the lead becomes one sheet, sent with every panel the lead
   is in.
4. Draw. Each panel is drawn from its reference crop, the sheet, the director's words and one block of standing
   instructions (style, no text, ages, kits, place, never mirrored). Batch orders send the first drawings as
   one Gemini Batch API job, like the Gemini-only book.
5. Review and fix. The director sees every drawing next to its direction and reference. A flagged panel gets
   an image edit ("change ONLY this") or a new drawing with the correction added; the director then keeps the
   better of the two. At most DIRECTOR_FIX_ROUNDS rounds and DIRECTOR_MAX_FIXES fixes a book.
6. Pages. Slanted panels, captions and sound effects where the director placed them, one PDF.

Every answer and every picture is saved in the order's folder under a fixed name, so a restarted order walks
the same steps and pays for nothing twice.
"""
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import db, director, pages, pipeline, settings, video as clips

# The look of each style in the directed book. The "slamdunk" look is the one the owner approved on the first
# directed book (2026-11-10): full colour, not the black-and-white of the Gemini-only book.
LOOKS = {"slamdunk": "full-colour Japanese shonen sports manga page art, the detailed kind used in classic basketball "
                     "manga: fine confident black ink lines with hatching, rich semi-realistic colouring with soft "
                     "watercolour-like shading and strong light, dynamic foreshortening, speed lines and impact lines "
                     "where they help the motion."}
NO_TEXT = ("NO TEXT: no captions, no speech bubbles, no sound effects, no letters or logos on walls, banners or the "
           "floor, no watermark, no scoreboard digits. The only writing allowed is jersey numbers.")
REF_NOTE = ("Image 1 is the real video frame for this moment. It is small and blurry: use it for who stands where "
            "and for the poses, and ignore any lettering or graphics in it. Do not copy its distant camera; stage "
            "the panel as described below.")
SHEET_NOTE = ("Image {n} is the character sheet of the lead: wherever the lead appears, draw the lead exactly like "
              "the sheet (face, hair, kit, number, shoes). Copy nothing else from it.")
SHEET_PROMPT = (
    "Image 1 is a strip of {n} zoomed pictures of the same player, taken from a real video; they are small and "
    "blurry. Draw a character reference sheet of this ONE person for a manga: a full-body front view, a full-body "
    "back view, a side view and a head close-up, side by side on a plain white background. The lead: {lead}. Keep "
    "the real kit colours and the number.{photo} No other people. No text or labels anywhere except the jersey "
    "number.")
EDIT_PROMPT = ("Edit image 1, a manga panel. Keep the composition, camera, people, poses, colours and drawing style "
               "exactly as they are; change ONLY this: {fix}{sheet} Output one single illustration with no text.")
REDRAW_NOTE = "\nCORRECTIONS (the last drawing got these wrong; get them right this time): {fix}"


def enabled() -> bool:
    return director.enabled()


# ---------- study ----------

def _saved(path: Path, make):
    """The JSON at path, or make() saved there: a restarted order doesn't ask (and pay) twice."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = make()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return value


def _times(dur: float, fps: float) -> list[float]:
    return [pipeline._clamp_t(k / fps, dur) for k in range(max(1, int(dur * fps + 0.5)))]


def _hoop_tile(full: Image.Image, hoop, t: float, tile: int = 200) -> Image.Image:
    """A time-labelled close-up of the basket: the rim is a few dozen pixels wide in the clip, and whether the
    ball goes through is the one fact the book must not get wrong."""
    y0, x0, y1, x1 = hoop
    cy, cx, half_h, half_w = (y0 + y1) / 2, (x0 + x1) / 2, max(y1 - y0, 120) * 0.9, max(x1 - x0, 80) * 0.9
    W, H = full.size
    part = full.crop((int(max(0, cx - half_w) * W / 1000), int(max(0, cy - half_h) * H / 1000),
                      int(min(1000, cx + half_w) * W / 1000), int(min(1000, cy + half_h) * H / 1000)))
    part = part.resize((tile, tile), Image.LANCZOS)
    ImageDraw.Draw(part).text((6, 4), f"{t:.2f}", font=pages.font("caption", 24), fill="yellow",
                              stroke_width=3, stroke_fill="black")
    return part


def _tile_sheets(tiles: list[Image.Image], cols: int = 6, per_sheet: int = 60) -> list[Image.Image]:
    sheets = []
    for start in range(0, len(tiles), per_sheet):
        chunk = tiles[start:start + per_sheet]
        w, h = chunk[0].size
        sheet = Image.new("RGB", (cols * w, -(-len(chunk) // cols) * h), "black")
        for k, part in enumerate(chunk):
            sheet.paste(part, ((k % cols) * w, (k // cols) * h))
        sheets.append(sheet)
    return sheets


def study(dc, order: dict, video: Path, work: Path, sleep=time.sleep, progress=lambda p: None) -> dict:
    """The director's plan for this clip, normalised and with every box on the full frame."""
    dur, _, _ = clips.probe(video)
    oid = order["id"]
    lang = "Simplified Chinese" if order.get("lang") == "zh" else "English"

    def frame(t: float, overlays=None) -> Image.Image:
        name = f"t_{t:.2f}.jpg" if overlays is not None else f"raw_{t:.2f}.jpg"
        with Image.open(pipeline.still(video, t, work / "src" / name, overlays or [])) as img:
            return img.convert("RGB")

    def located() -> dict:
        times = _times(dur, min(settings.DIRECTOR_LOCATE_FPS, 40 / max(dur, 0.1)))
        return director.locate(dc, oid, [(t, frame(t)) for t in times], dur, sleep)

    where = _saved(work / "locate.json", located)
    progress(5)
    overlays = [b for b in where.get("overlays") or [] if pipeline._box(b)]
    track = [m for m in where.get("track") or [] if isinstance(m, dict)]

    fps = min(settings.DIRECTOR_ZOOM_FPS, settings.DIRECTOR_MAX_FRAMES / max(dur, 0.1))
    times = _times(dur, fps)
    size = frame(times[0], overlays).size
    rects = [pipeline.follow_box(track, t, size, settings.DIRECTOR_ZOOM_FRAC) for t in times]

    def planned() -> dict:
        hoop = pipeline._box(where.get("hoop"))
        step = max(1, round(2 * fps))                       # a whole frame every two seconds, for orientation
        zooms, wholes, tiles = [], [], []
        for k, (t, rect) in enumerate(zip(times, rects)):   # one full frame in memory at a time
            full = frame(t, overlays)
            zoom = full.crop(rect)
            zoom.thumbnail((settings.DIRECTOR_ZOOM_PX, settings.DIRECTOR_ZOOM_PX))
            zooms.append((t, zoom))
            if k % step == 0:
                small = full.copy()
                small.thumbnail((640, 640))
                wholes.append((t, small))
            if hoop:
                tiles.append(_hoop_tile(full, hoop, t))
        return director.plan(dc, oid, zooms, wholes, _tile_sheets(tiles),
                             {"gist": where.get("gist") or "", "on_screen_text": where.get("on_screen_text") or ""},
                             dur, fps, pipeline.THEME_TONE.get(order.get("theme"), pipeline.THEME_TONE["sports"]),
                             lang, sleep)

    raw = _saved(work / "director_plan.json", planned)
    return normalise(raw, where, times, rects, size, dur)


def _nearest(times: list[float], t: float) -> int:
    return min(range(len(times)), key=lambda k: abs(times[k] - t))


def normalise(raw: dict, where: dict, times: list[float], rects: list, size: tuple[int, int], dur: float) -> dict:
    """Keep what the pages and prompts can rely on: at most 4 rows a page, 2 cells a row, 2 panels a cell; art
    panels with a direction, text panels with words. Boxes come on the zoom frame nearest their time and are
    turned into boxes on the full frame. Captions about whistles, fouls or calls are dropped."""
    def full_box(t: float, box) -> list[float] | None:
        if not pipeline._box(box):
            return None
        return pipeline._zoom_to_full(box, rects[_nearest(times, t)], size)

    book, count, texts = [], 0, 0
    for page in raw.get("pages") or []:
        rows = []
        for row in (page or {}).get("rows") or []:
            cells = []
            for cell in ((row or {}).get("cells") or [])[:2]:
                kept = []
                for p in ((cell or {}).get("panels") or [])[:2]:
                    if not isinstance(p, dict):
                        continue
                    if p.get("kind") == "text":
                        words = str(p.get("words") or "").strip()
                        if not words or texts >= 2:
                            continue
                        texts += 1
                        kept.append({"kind": "text", "words": words})
                        continue
                    direction = str(p.get("direction") or "").strip()
                    if not direction:
                        continue
                    t = pipeline._clamp_t(p.get("t"), dur)
                    panel = {"kind": "art", "t": t, "direction": direction, "shows_lead": bool(p.get("shows_lead")),
                             "box": full_box(t, p.get("box")) if p.get("use_ref", True) else None}
                    for field in ("caption", "sfx"):
                        value = str(p.get(field) or "").strip()
                        panel[field] = "" if pipeline.UNSEEN.search(value) else value
                    kept.append(panel)
                if kept:
                    cells.append({"panels": kept})
            if cells:
                rows.append({"height": row.get("height") if row.get("height") in pages.HEIGHTS else "normal",
                             "split": row.get("split") if row.get("split") in pages.SPLITS else "even",
                             "cells": cells})
        rows = rows[:4]
        for row in rows:
            for cell in row["cells"]:
                for panel in cell["panels"]:
                    count += 1
                    panel["id"] = f"D{len(book) + 1}-{count}"
        if rows:
            book.append({"rows": rows})
    if not any(p["kind"] == "art" for p in _panels({"pages": book})):
        raise director.DirectorError("The director's plan came back without panels")
    cast = raw.get("cast") if isinstance(raw.get("cast"), dict) else {}
    views = []
    for view in raw.get("lead_views") or []:
        if isinstance(view, dict) and pipeline._is_number(view.get("t")):
            t = pipeline._clamp_t(view["t"], dur)
            box = full_box(t, view.get("box"))
            if box:
                views.append({"t": t, "box": box})
    return {"title": str(raw.get("title") or ""), "facts": raw.get("facts") or {},
            "cast": {"people": str(cast.get("people") or ""), "lead": str(cast.get("lead") or ""),
                     "kits": [str(k) for k in cast.get("kits") or []],
                     "others": [str(o) for o in cast.get("others") or []],
                     "place": str(cast.get("place") or ""), "ball": str(cast.get("ball") or "")},
            "lead_views": views[:4], "overlays": [b for b in where.get("overlays") or [] if pipeline._box(b)],
            "on_screen_text": where.get("on_screen_text") or "", "pages": book}


def _panels(plan: dict) -> list[dict]:
    return [p for page in plan["pages"] for row in page["rows"] for cell in row["cells"] for p in cell["panels"]]


# ---------- prompts ----------

def style_block(style: str, cast: dict) -> str:
    look = LOOKS.get(style) or f"{pipeline.STYLE_PROMPTS.get(style, pipeline.STYLE_PROMPTS['slamdunk'])}; full colour."
    lines = [f"STYLE: {look} Cinematic camera, never a flat security-camera view.", NO_TEXT]
    if cast.get("people"):
        lines.append(f"PEOPLE: {cast['people']}")
    if cast.get("kits"):
        lines.append("KITS: " + " ".join(cast["kits"]))
    who = ([f"The lead: {cast['lead']}."] if cast.get("lead") else []) + list(cast.get("others") or [])
    if who:
        lines.append("CAST: " + " ".join(who))
    if cast.get("place") or cast.get("ball"):
        lines.append("PLACE: " + " ".join(x for x in (cast.get("place"), cast.get("ball")) if x))
    lines.append("DIRECTIONS are as seen in the picture (screen-left, screen-right). Do not mirror the scene.")
    return "\n".join(lines)


def panel_prompt(panel: dict, aspect: str, style_text: str, has_ref: bool, has_sheet: bool) -> str:
    lines = [REF_NOTE] if has_ref else []
    if has_sheet:
        lines.append(SHEET_NOTE.format(n=2 if has_ref else 1))
    lines += [f"PANEL: {panel['direction']}",
              f"One single manga panel, aspect ratio {aspect}; the picture fills the whole frame, with no border.",
              "", style_text]
    return "\n".join(lines)


# ---------- references, sheet, jobs ----------

def _reference(full: Image.Image, box, longest: int = 1280, min_frac: float = 0.18) -> Image.Image:
    """The part of the full frame around box, in the box's own shape (within reason), enlarged."""
    W, H = full.size
    y0, x0, y1, x1 = pipeline._box(box)
    shape = min(2.0, max(0.6, ((x1 - x0) * W) / max(1.0, (y1 - y0) * H)))
    part = full.crop(pipeline._crop_rect(full.size, box, shape, min_frac, 0.15))
    scale = longest / max(part.size)
    return part.resize((round(part.width * scale), round(part.height * scale)), Image.LANCZOS)


def _still(video: Path, work: Path, t: float, overlays) -> Image.Image:
    with Image.open(pipeline.still(video, t, work / "src" / f"t_{t:.2f}.jpg", overlays)) as img:
        return img.convert("RGB")


def draw_sheet(gemini, order: dict, plan: dict, video: Path, work: Path, sleep=time.sleep) -> Path | None:
    """One character sheet of the lead, from a strip of the crops the director picked."""
    out = work / "sheet.png"
    if out.exists():
        return out
    crops = []
    for view in plan["lead_views"]:
        part = _reference(_still(video, work, view["t"], plan["overlays"]), view["box"], 640, 0.08)
        crops.append(part.resize((round(part.width * 640 / part.height), 640), Image.LANCZOS))
    if not crops:
        return None
    strip = Image.new("RGB", (sum(c.width for c in crops) + 12 * (len(crops) - 1), 640), "white")
    x = 0
    for part in crops:
        strip.paste(part, (x, 0))
        x += part.width + 12
    strip.save(work / "lead_strip.jpg", quality=92)
    photo = Path(order["photo"]) if order.get("photo") else None
    prompt = SHEET_PROMPT.format(n=len(crops), lead=plan["cast"]["lead"] or "the main player",
                                 photo=f" {pipeline.PHOTO_NOTE}" if photo else "")
    images = [pipeline._part(strip, 1600)] + ([pipeline._part(photo, 1024)] if photo else [])
    art = pipeline._image_call(gemini, images + [prompt + "\n\n" + style_block(order["style"], plan["cast"])],
                               "16:9", sleep, settings.BOOK_IMAGE_SIZE)
    pipeline._save(art, out)
    db.add_cost(order["id"], "character sheet", settings.GEMINI_IMAGE_MODEL, 1, pipeline.PANEL_PRICE["standard"])
    return out


def panel_jobs(plan: dict, order: dict, video: Path, work: Path, sheet: Path | None) -> list[dict]:
    """One entry per panel, in reading order, with its shape on the page. Art panels also get their key (the
    PNG's name), aspect, reference crop, pictures and prompt, in the form pipeline.draw_standard and
    pipeline.draw_batch take."""
    style_text = style_block(order["style"], plan["cast"])
    jobs = []
    for pi, page in enumerate(plan["pages"]):
        spec = [{"height": r["height"], "split": r["split"], "cells": [len(c["panels"]) for c in r["cells"]]}
                for r in page["rows"]]
        panels = [p for r in page["rows"] for c in r["cells"] for p in c["panels"]]
        for panel, shape in zip(panels, pages.page_shapes(spec, flip=pi % 2 == 1)):
            job = {"key": panel["id"], "page": pi, "panel": panel, "shape": shape}
            if panel["kind"] == "art":
                ref = None
                if panel.get("box"):
                    ref = work / "ref" / f"{panel['id']}.jpg"
                    if not ref.exists():
                        ref.parent.mkdir(parents=True, exist_ok=True)
                        _reference(_still(video, work, panel["t"], plan["overlays"]), panel["box"]).save(ref, quality=92)
                with_sheet = bool(sheet and panel.get("shows_lead"))
                aspect = pipeline.nearest_aspect(pages.ratio(shape))
                job.update(aspect=aspect, ref=ref, size=settings.BOOK_IMAGE_SIZE,
                           images=([ref] if ref else []) + ([sheet] if with_sheet else []),
                           prompt=panel_prompt(panel, aspect, style_text, bool(ref), with_sheet))
            jobs.append(job)
    return jobs


# ---------- review and fix ----------

def _fix(gemini, job: dict, current: Path, verdict: dict, sheet: Path | None, sleep) -> Image.Image:
    fix = str(verdict.get("fix") or "; ".join(verdict.get("problems") or [])).strip()
    if verdict.get("verdict") == "redraw":
        again = dict(job, prompt=job["prompt"] + REDRAW_NOTE.format(fix=fix))
        return pipeline.draw_panel(gemini, again, "", {}, None, sleep)
    with_sheet = bool(sheet and job["panel"].get("shows_lead"))
    prompt = EDIT_PROMPT.format(fix=fix, sheet=" Image 2 is the lead's character sheet, for reference only."
                                if with_sheet else "")
    images = [pipeline._part(current, 1600)] + ([pipeline._part(sheet, 1600)] if with_sheet else [])
    return pipeline._image_call(gemini, images + [prompt], job["aspect"], sleep, job.get("size"))


def review_and_fix(dc, gemini, order: dict, plan: dict, jobs: list[dict], sheet: Path | None, work: Path,
                   progress=lambda p: None, sleep=time.sleep) -> tuple[dict, dict]:
    """The drawing kept for each panel and the director's last verdict on it (with the lettering), by key."""
    art_jobs = [j for j in jobs if j["panel"]["kind"] == "art"]
    style_text = style_block(order["style"], plan["cast"])
    oid = order["id"]

    def entry(job, new, old=None):
        return {"id": job["key"], "direction": job["panel"]["direction"], "ref": job.get("ref"), "new": new, "old": old}

    current = {j["key"]: work / f"{j['key']}.png" for j in art_jobs}
    verdicts = _saved(work / "review_0.json", lambda: director.review(
        dc, oid, "director (review)", [entry(j, current[j["key"]]) for j in art_jobs], sheet, plan["facts"],
        style_text, sleep))
    progress(88)
    used = 0
    for rnd in range(1, settings.DIRECTOR_FIX_ROUNDS + 1):
        flagged = [j for j in art_jobs if (verdicts.get(j["key"]) or {}).get("verdict") in ("edit", "redraw")]
        flagged = flagged[:max(0, settings.DIRECTOR_MAX_FIXES - used)]
        if not flagged:
            break
        fixes = {}
        for job in flagged:
            out = work / f"{job['key']}.fix{rnd}.png"
            if not out.exists():
                verdict = verdicts[job["key"]]
                pipeline._save(_fix(gemini, job, current[job["key"]], verdict, sheet, sleep), out)
                db.add_cost(oid, "panel redraw" if verdict.get("verdict") == "redraw" else "panel fix",
                            settings.GEMINI_IMAGE_MODEL, 1, pipeline.PANEL_PRICE["standard"])
            fixes[job["key"]] = out
            used += 1
        second = _saved(work / f"review_{rnd}.json", lambda: director.review(
            dc, oid, f"director (review of fixes {rnd})",
            [entry(j, fixes[j["key"]], current[j["key"]]) for j in flagged], sheet, plan["facts"], style_text, sleep))
        for job in flagged:
            verdict = second.get(job["key"])
            if not verdict:
                continue                   # no answer for this panel: the first drawing and its lettering stay
            if verdict.get("keep") != "old":
                current[job["key"]] = fixes[job["key"]]
            verdicts[job["key"]] = verdict
        progress(88 + 7 * rnd / settings.DIRECTOR_FIX_ROUNDS)
    return current, verdicts


# ---------- the order ----------

def _point(value, default: tuple[float, float]) -> tuple[float, float]:
    try:
        x, y = (min(1.0, max(0.0, float(v) / 1000)) for v in value)
        return x, y
    except (TypeError, ValueError):
        return default


def page_items(jobs: list[dict], current: dict, verdicts: dict, page: int) -> list[dict]:
    items = []
    for job in (j for j in jobs if j["page"] == page):
        panel = job["panel"]
        if panel["kind"] == "text":
            items.append({"kind": "text", "shape": job["shape"], "words": panel["words"]})
            continue
        v = verdicts.get(job["key"]) or {}
        try:
            angle = float(v.get("sfx_angle") or 0)
        except (TypeError, ValueError):
            angle = 0.0
        items.append({"kind": "art", "shape": job["shape"], "art": current[job["key"]],
                      "focus": _point(v.get("focus"), (0.5, 0.45)), "caption": panel.get("caption") or "",
                      "corner": v.get("corner") if v.get("corner") in ("tl", "tr", "bl", "br") else "tl",
                      "sfx": panel.get("sfx") or "", "sfx_at": _point(v.get("sfx_at"), (0.72, 0.82)),
                      "sfx_size": v.get("sfx_size") or "medium", "sfx_angle": angle,
                      "sfx_colour": v.get("sfx_colour") or "white"})
    return items


def run(order: dict, progress=lambda p: None, gemini=None, sleep=time.sleep, now=time.time, dc=None) -> Path:
    work = settings.DATA_DIR / "jobs" / order["id"]
    work.mkdir(parents=True, exist_ok=True)
    video = Path(order["video"])
    gemini = gemini or pipeline._client()
    dc = dc or director.client()
    plan = study(dc, order, video, work, sleep, progress)
    (work / "directed.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    progress(18)
    sheet = draw_sheet(gemini, order, plan, video, work, sleep)
    jobs = panel_jobs(plan, order, video, work, sheet)
    art_jobs = [j for j in jobs if j["panel"]["kind"] == "art"]
    progress(20)
    if order.get("api_style") == "batch":
        pipeline.draw_batch(gemini, order, art_jobs, plan, sheet, work, progress, sleep, now)
    pipeline.draw_standard(gemini, order, art_jobs, plan, sheet, work, progress, sleep)   # what a batch missed
    current, verdicts = review_and_fix(dc, gemini, order, plan, jobs, sheet, work, progress, sleep)
    sheets = []
    for pi in range(len(plan["pages"])):
        page = pages.build_page(page_items(jobs, current, verdicts, pi), pi + 1)
        page.save(work / f"page_{pi + 1}.jpg", quality=92)
        sheets.append(page)
    progress(97)
    pdf = work / "manga.pdf"
    sheets[0].save(pdf, save_all=True, append_images=sheets[1:], resolution=200)
    return pdf
