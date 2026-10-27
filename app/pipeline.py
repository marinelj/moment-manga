"""Video -> manga pipeline.

1. Pull frames from the clip with ffmpeg.
2. Ask a Gemini text model to storyboard 3-5 pages from the frames (JSON).
3. Draw each panel with the Gemini image model, using the matching frame as pose reference.
4. Letter captions and SFX with Pillow, lay out pages, save JPGs + one PDF.

GEMINI_API_KEY is read from the environment (see settings.py).
"""
import json
import subprocess
import textwrap
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import settings

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


def storyboard(client, frames: list[Path], theme: str, lang: str) -> dict:
    from google.genai import types
    prompt = f"""You are a manga editor. These {len(frames)} frames come from one short video, in order (frame 0 first).
Plan a 3 to 5 page manga telling {THEME_TONE[theme]}, using only what actually happens in the frames.
Each page has 3 or 4 panels. Write captions and sound effects in {lang}; keep each caption under 20 characters.
Return JSON: {{"title": str, "pages": [{{"panels": [{{"frame": int, "shot": "wide|medium|close-up",
"description": str (what to draw, in English), "caption": str or "", "sfx": str or ""}}]}}]}}"""
    parts = [prompt] + [Image.open(f) for f in frames]
    r = client.models.generate_content(model=settings.GEMINI_TEXT_MODEL, contents=parts,
                                       config=types.GenerateContentConfig(response_mime_type="application/json"))
    return json.loads(r.text)


def draw_panel(client, frame: Path, panel: dict, style: str, aspect: str) -> Image.Image:
    from google.genai import types
    prompt = (f"Draw ONE single manga panel ({panel['shot']} shot) in a {STYLE_PROMPTS[style]}. "
              f"Scene: {panel['description']}. Use the photo only as pose and composition reference. "
              "No text, no speech bubbles, no sound effects, no logos; jersey numbers only. "
              f"Aspect ratio {aspect}.")
    r = client.models.generate_content(
        model=settings.GEMINI_IMAGE_MODEL, contents=[prompt, Image.open(frame)],
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


def run(order: dict, progress=lambda p: None) -> Path:
    work = settings.DATA_DIR / "jobs" / order["id"]
    frames = extract_frames(Path(order["video"]), work / "frames")
    progress(8)
    client = _client()
    lang = "Simplified Chinese" if order.get("lang") == "zh" else "English"
    plan = storyboard(client, frames, order["theme"], lang)
    progress(15)
    total = sum(len(p["panels"]) for p in plan["pages"])
    done, pages = 0, []
    for pi, page in enumerate(plan["pages"]):
        sheet = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        d = ImageDraw.Draw(sheet)
        for panel, (x, y, w, h) in zip(page["panels"], layout(len(page["panels"]))):
            frame = frames[max(0, min(int(panel.get("frame", 0)), len(frames) - 1))]
            aspect = "16:9" if w / h > 1.3 else "4:3" if w / h > 0.9 else "3:4"
            art = draw_panel(client, frame, panel, order["style"], aspect)
            art.save(work / f"P{pi + 1}-{done + 1}.png")
            art = letter(_cover(art, w, h), panel.get("caption", ""), panel.get("sfx", ""))
            sheet.paste(art, (x, y))
            d.rectangle([x, y, x + w, y + h], outline="black", width=6)
            done += 1
            progress(15 + 80 * done / total)
        sheet.save(work / f"page_{pi + 1}.jpg", quality=92)
        pages.append(sheet)
    pdf = work / "manga.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=200)
    return pdf


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))
