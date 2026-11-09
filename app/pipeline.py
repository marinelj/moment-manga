"""Video -> manga pipeline.

1. Pull frames from the clip with ffmpeg.
2. Ask a Gemini text model to storyboard 3-5 pages from the frames (JSON).
3. Draw each panel with the Gemini image model, using the matching frame as pose reference.
   Orders made in the "batch" API style send all panels as one Gemini Batch API job at half price;
   anything the job doesn't return is drawn with standard calls.
4. Letter captions and SFX with Pillow, lay out pages, save JPGs + one PDF.

The storyboard and every finished panel are saved in the order's folder, and so is the batch job's name.
A restarted order reuses them, so it doesn't pay for the same panel, or the same batch job, twice.

GEMINI_API_KEY is read from the environment (see settings.py).
"""
import base64
import json
import subprocess
import textwrap
import time
import traceback
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import db, settings

STYLE_PROMPTS = {
    "miyazaki": "hand-painted Japanese animated-film look: soft watercolour backgrounds, clean warm line art, "
                "fresh vivid greens and sky blues, gentle light, expressive but natural faces",
    "naruto": "bold shonen ninja-action manga look: thick dynamic ink lines, strong orange and navy accents, "
              "dramatic angles, speed lines",
    "slamdunk": "1990s basketball sports manga look: realistic anatomy, precise heavy black ink, screentone shading, "
                "intense close-ups, motion lines",
    "demonslayer": "modern dark-fantasy manga look: crisp lines, rich teal, crimson and gold, "
                   "ukiyo-e inspired water and flame effects, traditional checkered textile patterns",
    "onepiece": "exuberant adventure manga look: rubbery exaggerated poses, huge expressions, bright ocean blues, "
                "sunny yellows and reds, playful thick outlines",
}
THEME_TONE = {
    "sports": "a dramatic sports moment with tension and a big payoff",
    "travel": "a warm travel or home-life slice-of-life story",
    "portrait": "a quiet, flattering character portrait story focused on faces and emotion",
    "comedy": "a light comedy with a punchline in the last panel",
}
PAGE_W, PAGE_H, MARGIN, GUTTER = 1654, 2339, 70, 28
# Cost per panel for GEMINI_IMAGE_MODEL (gemini-3-pro-image, 1K), USD, measured 2026-11-09: 1,120 image tokens
# at $120/M ($0.134) plus about 650 input tokens at $2/M and 380 thinking/text tokens at $12/M. The Batch API
# is documented at half price. Keep in step with the Gemini pricing page.
PANEL_PRICE = {"standard": 0.140, "batch": 0.070}
# GEMINI_TEXT_MODEL (gemini-3.8-flash) per token: $0.75/M in, $3.75/M out incl. thinking, until 2026-12-31.
# Google doubles both on 2027-01-01. A 24-frame storyboard is about 26.5k tokens in, 1.7k out: about $0.03.
STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT = 0.75 / 1e6, 3.75 / 1e6
BATCH_DONE = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
PHOTO_NOTE = "Keep the lead character's appearance consistent with this portrait reference."


def _client():
    from google import genai
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


def extract_frames(video: Path, out: Path, max_frames: int = 36) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)]).strip() or 10)
    fps = min(2.0, max_frames / max(dur, 1))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps={fps:.3f},scale=768:-2",
                    "-frames:v", str(max_frames), str(out / "f_%03d.jpg")], check=True)
    return sorted(out.glob("f_*.jpg"))


def storyboard(client, frames: list[Path], theme: str, lang: str, oid: str | None = None) -> dict:
    from google.genai import types
    prompt = f"""You are a manga editor. These {len(frames)} frames come from one short video, in order (frame 0 first).
Plan a 3 to 5 page manga telling {THEME_TONE[theme]}, using only what actually happens in the frames.
Each page has 3 or 4 panels. Write captions and sound effects in {lang}; keep each caption under 20 characters.
Return JSON: {{"title": str, "pages": [{{"panels": [{{"frame": int, "shot": "wide|medium|close-up",
"description": str (what to draw, in English), "caption": str or "", "sfx": str or ""}}]}}]}}"""
    parts = [prompt] + [Image.open(f) for f in frames]
    r = client.models.generate_content(model=settings.GEMINI_TEXT_MODEL, contents=parts,
                                       config=types.GenerateContentConfig(response_mime_type="application/json"))
    u = getattr(r, "usage_metadata", None)
    if oid and u:
        tin = u.prompt_token_count or 0
        tout = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        db.add_cost(oid, "storyboard", settings.GEMINI_TEXT_MODEL, tin + tout,
                    tin * STORYBOARD_PRICE_IN + tout * STORYBOARD_PRICE_OUT)
    return json.loads(r.text)


def panel_prompt(panel: dict, style: str, aspect: str) -> str:
    return (f"Draw ONE single manga panel ({panel['shot']} shot) in a {STYLE_PROMPTS[style]}. "
            f"Scene: {panel['description']}. Use the photo only as pose and composition reference. "
            "No text, no speech bubbles, no sound effects, no logos; jersey numbers only. "
            f"Aspect ratio {aspect}.")


def draw_panel(client, frame: Path, panel: dict, style: str, aspect: str,
               photo: Path | None = None) -> Image.Image:
    from google.genai import types
    references = [panel_prompt(panel, style, aspect), Image.open(frame)]
    if photo:
        references.extend([PHOTO_NOTE, Image.open(photo)])
    r = client.models.generate_content(
        model=settings.GEMINI_IMAGE_MODEL, contents=references,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]))
    for part in r.candidates[0].content.parts:
        if part.inline_data:
            return Image.open(BytesIO(part.inline_data.data)).convert("RGB")
    raise RuntimeError("Image model returned no image")


def _font(size):
    try:
        return ImageFont.truetype(settings.FONT_PATH, size)
    except OSError:
        return ImageFont.load_default(size)


def letter(img: Image.Image, caption: str, sfx: str) -> Image.Image:
    d = ImageDraw.Draw(img)
    if caption:
        f = _font(max(28, img.width // 22))
        lines = textwrap.wrap(caption, 12) or [caption]
        lh = f.size + 8
        w = max(d.textlength(t, font=f) for t in lines) + 36
        h = lh * len(lines) + 24
        d.rectangle([14, 14, 14 + w, 14 + h], fill="white", outline="black", width=4)
        for i, t in enumerate(lines):
            d.text((32, 26 + i * lh), t, font=f, fill="black")
    if sfx:
        f = _font(max(48, img.width // 9))
        d.text((img.width - 30, img.height - 30), sfx, font=f, fill="white", anchor="rb",
               stroke_width=8, stroke_fill="black")
    return img


def layout(n: int) -> list[tuple[int, int, int, int]]:
    """Panel boxes for a page with n panels: one wide top panel, then a row of two, then wide."""
    x0, y0, w, h = MARGIN, MARGIN, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN
    rows = {1: [1], 2: [1, 1], 3: [1, 2], 4: [1, 2, 1]}[max(1, min(n, 4))]
    rh = (h - GUTTER * (len(rows) - 1)) / len(rows)
    boxes = []
    for r, cols in enumerate(rows):
        cw = (w - GUTTER * (cols - 1)) / cols
        for c in range(cols):
            boxes.append((int(x0 + c * (cw + GUTTER)), int(y0 + r * (rh + GUTTER)), int(cw), int(rh)))
    return boxes


def run(order: dict, progress=lambda p: None, client=None, sleep=time.sleep, now=time.time) -> Path:
    work = settings.DATA_DIR / "jobs" / order["id"]
    frames = extract_frames(Path(order["video"]), work / "frames")
    progress(8)
    client = client or _client()
    lang = "Simplified Chinese" if order.get("lang") == "zh" else "English"
    plan_file = work / "plan.json"
    if plan_file.exists():      # a restarted order keeps its storyboard, so saved panels and batch results still fit
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    else:
        plan = storyboard(client, frames, order["theme"], lang, order["id"])
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    progress(15)
    jobs = panel_jobs(plan, frames)
    photo = Path(order["photo"]) if order.get("photo") else None
    if order.get("api_style") == "batch":
        draw_batch(client, order, jobs, photo, work, progress, sleep, now)
    draw_standard(client, order, jobs, photo, work, progress)    # after a batch: only what it didn't return
    pages = []
    for pi, page in enumerate(plan["pages"]):
        sheet = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        d = ImageDraw.Draw(sheet)
        for job in (j for j in jobs if j["page"] == pi):
            x, y, w, h = job["box"]
            art = Image.open(work / f"{job['key']}.png").convert("RGB")
            art = letter(_cover(art, w, h), job["panel"].get("caption", ""), job["panel"].get("sfx", ""))
            sheet.paste(art, (x, y))
            d.rectangle([x, y, x + w, y + h], outline="black", width=6)
        sheet.save(work / f"page_{pi + 1}.jpg", quality=92)
        pages.append(sheet)
    progress(97)
    pdf = work / "manga.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=200)
    return pdf


def panel_jobs(plan: dict, frames: list[Path]) -> list[dict]:
    """One entry per panel to draw: its key (also the PNG name), page, box on the page, frame and aspect."""
    jobs = []
    for pi, page in enumerate(plan["pages"]):
        for panel, box in zip(page["panels"], layout(len(page["panels"]))):
            w, h = box[2], box[3]
            jobs.append({"key": f"P{pi + 1}-{len(jobs) + 1}", "page": pi, "panel": panel, "box": box,
                         "frame": frames[max(0, min(int(panel.get("frame", 0)), len(frames) - 1))],
                         "aspect": "16:9" if w / h > 1.3 else "4:3" if w / h > 0.9 else "3:4"})
    return jobs


def _save(img: Image.Image, out: Path) -> None:
    tmp = out.with_suffix(".tmp.png")       # never leave half a PNG behind for a restarted order to reuse
    img.save(tmp)
    tmp.replace(out)


def draw_standard(client, order: dict, jobs: list[dict], photo: Path | None, work: Path, progress) -> None:
    """Interactive calls, one panel at a time. Panels already on disk are kept."""
    todo = [j for j in jobs if not (work / f"{j['key']}.png").exists()]
    for n, job in enumerate(todo, 1):
        art = draw_panel(client, job["frame"], job["panel"], order["style"], job["aspect"], photo)
        _save(art, work / f"{job['key']}.png")
        db.add_cost(order["id"], "book panel", settings.GEMINI_IMAGE_MODEL, 1, PANEL_PRICE["standard"])
        progress(15 + 80 * (len(jobs) - len(todo) + n) / len(jobs))


# ---------- Gemini Batch API ----------

def draw_batch(client, order: dict, jobs: list[dict], photo: Path | None, work: Path, progress,
               sleep=time.sleep, now=time.time) -> int:
    """Draw the missing panels as one Gemini Batch API job and save what comes back. Returns how many.

    The job's name is saved before waiting, so after a restart the order waits on the same job instead of
    paying for a second one. A job still unfinished after BATCH_MAX_WAIT_HOURS is cancelled; whatever it
    didn't return (failed requests, an expired or cancelled job) is left for draw_standard.
    """
    todo = [j for j in jobs if not (work / f"{j['key']}.png").exists()]
    if not todo:
        return 0
    state_file = work / "batch.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
    else:
        state = _submit_batch(client, order, todo, photo, work, now)
        state_file.write_text(json.dumps(state))
    progress(20)
    job = _wait_batch(client, state, sleep, now)
    return _collect_batch(client, job, order, todo, work)


def _jpeg_b64(path: Path, longest: int | None = None) -> str:
    if longest is None and path.suffix.lower() in (".jpg", ".jpeg"):
        return base64.b64encode(path.read_bytes()).decode()
    with Image.open(path) as img:
        img = img.convert("RGB")
        if longest:
            img.thumbnail((longest, longest))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _submit_batch(client, order: dict, todo: list[dict], photo: Path | None, work: Path, now) -> dict:
    from google.genai import types
    # The portrait goes into every request, so shrink it to keep the request file small.
    photo_b64 = _jpeg_b64(photo, 1024) if photo else None
    requests = work / "batch_requests.jsonl"
    with requests.open("w", encoding="utf-8") as f:
        for job in todo:
            parts = [{"text": panel_prompt(job["panel"], order["style"], job["aspect"])},
                     {"inlineData": {"mimeType": "image/jpeg", "data": _jpeg_b64(job["frame"])}}]
            if photo_b64:
                parts += [{"text": PHOTO_NOTE}, {"inlineData": {"mimeType": "image/jpeg", "data": photo_b64}}]
            f.write(json.dumps({"key": job["key"], "request": {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"]}}}) + "\n")
    name = f"mm-{order['id'][:12]}-panels"
    uploaded = client.files.upload(file=str(requests),
                                   config=types.UploadFileConfig(display_name=name, mime_type="jsonl"))
    job = client.batches.create(model=settings.GEMINI_IMAGE_MODEL, src=uploaded.name,
                                config={"display_name": name})
    return {"job": job.name, "submitted": now(), "keys": [j["key"] for j in todo]}


def _job_state(job) -> str:
    state = getattr(job, "state", None)
    return str(getattr(state, "name", state) or "")


def _wait_batch(client, state: dict, sleep, now):
    """The finished job, or after the deadline (or ten failed status checks in a row) whatever it has."""
    deadline = state["submitted"] + settings.BATCH_MAX_WAIT_HOURS * 3600
    errors = 0
    while True:
        try:
            job = client.batches.get(name=state["job"])
            errors = 0
            if _job_state(job) in BATCH_DONE:
                return job
        except Exception:
            traceback.print_exc()          # a network blip on the Mac or the tunnel: keep waiting
            errors += 1
            if errors >= 10:
                return None
        if now() >= deadline:
            try:
                client.batches.cancel(name=state["job"])
                return client.batches.get(name=state["job"])
            except Exception:
                traceback.print_exc()
                return None
        sleep(settings.BATCH_POLL_SECONDS)


def _image_from_response(response: dict) -> Image.Image | None:
    for candidate in response.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                try:
                    return Image.open(BytesIO(base64.b64decode(inline["data"]))).convert("RGB")
                except (ValueError, OSError):
                    return None
    return None


def _collect_batch(client, job, order: dict, todo: list[dict], work: Path) -> int:
    name = getattr(getattr(job, "dest", None), "file_name", None) if job else None
    if not name:
        return 0
    try:
        data = client.files.download(file=name)
    except Exception:
        traceback.print_exc()
        return 0
    wanted, got = {j["key"] for j in todo}, 0
    for line in (data.decode("utf-8") if isinstance(data, bytes) else str(data)).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = row.get("key") if isinstance(row, dict) else None
        if key not in wanted or (work / f"{key}.png").exists():
            continue
        art = _image_from_response(row.get("response") or {})
        if art is None:
            continue        # a failed request in the job: draw_standard draws it
        _save(art, work / f"{key}.png")
        db.add_cost(order["id"], "book panel (batch)", settings.GEMINI_IMAGE_MODEL, 1, PANEL_PRICE["batch"])
        got += 1
    return got


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))
