"""Video -> manga book pipeline.

The demo books were faithful because every panel was drawn from facts checked frame by frame, from a crop
that follows the lead, with a character sheet and a check-and-fix pass. This pipeline does the same:

1. Facts first. Sample the clip at STORY_FPS and ask the text model what actually happens: the teams and
   their kits, the lead, every event with its time and a box around it, how it ends, and the broadcast
   graphics. It must say "unclear" rather than guess, and never report a foul, whistle or reaction it can't see.
2. Zoom and plan. Cut a zoomed crop of every event from the full-resolution clip and ask again: correct the
   facts from the zooms, then plan the panels from those events only, each with an exact time and a box.
3. Each panel's source picture is cut from the full-resolution frame at its time, with the broadcast
   graphics painted out, cropped to the panel's box and shape.
4. One character sheet of the lead is drawn per order and sent with every panel, with the style reference,
   the cast's clothes colours, the players' age and a strict "redraw this picture" prompt (not flipped, no
   extra people, no text). Orders made in the "batch" API style send all panels as one Gemini Batch API job
   at half price; anything the job doesn't return is drawn with standard calls.
5. A cheap model checks every panel against its source and the sheet. A flagged panel gets one fix (an image
   edit, or a redraw if it came out mirrored or with extra people), at most BOOK_MAX_FIXES per book, kept only
   if a second check finds fewer problems.
6. Letter captions and SFX with Pillow, lay out pages, save JPGs + one PDF.

The facts, the plan, the sheet, every finished panel and every check are saved in the order's folder, and
so is the batch job's name. A restarted order reuses them, so it doesn't pay for the same call twice.

While ANTHROPIC_API_KEY is set, run() hands the order to the directed book instead (app/directed.py): Claude
studies the frames, directs and reviews, and this module's drawing, batch and frame helpers do the Gemini work.

GEMINI_API_KEY is read from the environment (see settings.py).
"""
import base64
import json
import math
import re
import subprocess
import textwrap
import time
import traceback
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from . import db, settings, video as clips

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
# Black-and-white styles keep only the clothes in colour, like the moving-manga video; the rest are full colour.
# Without this the model mixed colour and black-and-white panels in one book.
PALETTE = {"slamdunk": "black-and-white ink and screentone; only the jerseys and clothes keep their real colours"}
FULL_COLOUR = "full colour, with every jersey and clothes colour exactly as in the video"
# Styles that have a drawing-technique reference image (copy the technique only, never the content).
STYLE_REFS = {"slamdunk": lambda: settings.VIDEO_STYLE_REF}
THEME_TONE = {
    "sports": "a sports moment exactly as it happened, made exciting by framing and captions",
    "travel": "a warm travel or home-life slice-of-life story",
    "portrait": "a quiet, flattering character portrait story focused on faces and emotion",
    "comedy": "a light comedy with a punchline in the last panel",
}
PAGE_W, PAGE_H, MARGIN, GUTTER = 1654, 2339, 70, 28
# Aspect ratios the image model can draw; each panel asks for the one nearest its box on the page.
ASPECTS = ["21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16"]
# Cost per image call for GEMINI_IMAGE_MODEL (gemini-3-pro-image, 1K), USD, measured 2026-11-09: 1,120 image tokens
# at $120/M ($0.134) plus about 650 input tokens at $2/M and 380 thinking/text tokens at $12/M. The Batch API
# is documented at half price. Each extra reference image adds about 1,100 input tokens ($0.002). Keep in step
# with the Gemini pricing page.
PANEL_PRICE = {"standard": 0.140, "batch": 0.070}
# GEMINI_TEXT_MODEL (gemini-3.8-flash) per token: $0.75/M in, $3.75/M out incl. thinking, until 2026-12-31.
# Google doubles both on 2027-01-01. The two storyboard calls together are about 45-60k tokens in: about $0.05.
STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT = 0.75 / 1e6, 3.75 / 1e6
# VIDEO_CHECK_MODEL (gemini-3.5-flash-lite) per token, for the panel checks: about $0.001 a panel.
CHECK_PRICE_IN, CHECK_PRICE_OUT = clips.CHECK_PRICE_IN, clips.CHECK_PRICE_OUT
BATCH_DONE = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
PHOTO_NOTE = "The last image is a portrait of the lead: keep the lead's face and hair consistent with it."
DIRECTED_PX = 1600      # longest side of the pictures sent with a directed panel (reference crop, character sheet)

FACTS_PROMPT = """These {n} frames come from one short video, in order; frame k is at t = k/{fps:g} seconds ({dur:.1f} s in all).
Before anyone draws a manga from it, write down the facts. Report only what the frames show. Where something is too
small or blurry to tell, write "unclear" instead of guessing. Never report a foul, whistle, referee's call, contact,
injury, celebration or crowd reaction that you cannot see.
Answer JSON only:
{{"overlays": [[ymin, xmin, ymax, xmax], ...] boxes (0-1000 scale) around graphics drawn on top of the picture (score bar, channel logo, captions); [] if none,
 "on_screen_text": what the score bar or captions say, and whether it changes during the clip,
 "age": "children" | "teenagers" | "adults" | "mixed": how old the main people look,
 "groups": [{{"name": team or group name if shown, else "", "look": jersey or clothes colours and numbers}}],
 "others": [one line for each referee, coach or bystander who appears: clothes, where, what they do],
 "lead": {{"look": clothes colour, number if any, hair, build, "why": why this is the person to follow, e.g. the one who shoots}},
 "events": [{{"t": seconds, "what": what happens and who does it (by clothes and number), with screen-left/screen-right as seen in the frame; in a sport, the exact technique (pull-up jump shot, layup, floater, crossover, behind-the-back dribble, pass...), "box": [ymin, xmin, ymax, xmax] around the people involved at that time (0-1000 scale), "certain": true or false}}],
 "ending": how it ends, e.g. "the shot goes in at 7.8 s", "the shot misses", "unclear",
 "hoop": [ymin, xmin, ymax, xmax] box (0-1000 scale) around the basket (backboard and rim) that the main play attacks, or null if there is none}}
List 4 to 14 events in time order, covering the whole clip."""

PLAN_PROMPT = """You are a manga editor. These {n} frames follow the play of one short video ({dur:.1f} s), zoomed in on
where the action is: "Zoom frame k" is at t = k/{fps:g} s. Work only from what these frames show; where something is
too small or blurry to tell, say "unclear" instead of guessing.{hoop_note}
First, for EVERY frame, note who has the ball (clothes colour and number, or "in the air", "in the net", "loose",
"unclear") and what they do. Then write the facts from those notes: who the lead is (the person who makes the main
play, e.g. who takes the shot), each event with its time and the exact technique (pull-up jump shot, layup, floater,
crossover, pass...; a layup is laid in from beside the rim, a shot released further out while jumping straight up is
a jump shot), and how it ends (whether a shot goes in, and when). Never report a foul, whistle, call, contact,
injury, celebration or crowd reaction that you cannot see.
Then plan a manga telling {tone}, using ONLY those events, in time order. Each panel shows one zoom frame as it
is: same camera, same people in the same places; never move a player toward the camera, never invent a face, a
reaction or a close-up that the frame does not show. Follow the lead in most panels and give the ending its own big
panel. If you cannot see whether a shot goes in, end on the ball in the air rather than guessing. Never write a
caption or sound effect about a whistle, a foul, a call or a referee's decision (sound is not in the frames).
About one panel per event: 6 to 12 panels on 2 to 4 pages, 3 or 4 panels per page. Captions and sound effects in
{lang}; keep each caption under 20 characters, and only say what the facts show (no invented scores, names or rules).
Context from a first look at the whole picture (teams and on-screen text only): {context}
Answer JSON only:
{{"ball": [one short note per frame, in order],
 "facts": {{"lead": who the lead is and why, "events": [{{"t": seconds, "what": what happens}}], "ending": how it ends}},
 "title": str,
 "cast": {{"age": "children" | "teenagers" | "adults" | "mixed",
          "lead": one line, e.g. "black jersey number 5 with a white number, short black hair, slim teenage boy",
          "groups": [one line per team or group: name if shown, clothes colours],
          "others": [one line per adult or bystander who appears in a panel]}},
 "pages": [{{"panels": [{{"t": seconds (the exact moment to draw),
    "box": [ymin, xmin, ymax, xmax] the part of the zoom frame nearest to t that this panel shows (0-1000 scale): tight
           around one person for a close-up, around the action for a medium shot, the whole zoom frame for a wide shot,
    "shot": "wide|medium|close-up",
    "description": what to draw, in English: who is where (screen-left/right as in the video, never flipped), clothes
           colours and numbers, the exact pose and move, where the ball is,
    "caption": str or "", "sfx": str or ""}}]}}]}}"""

VERIFY_PROMPT = """Each "Panel k" below is a zoomed picture from a real video, followed by what a manga panel
drawn from it claims to show. For every panel, judge only what the picture shows: "yes" if it supports the claim,
"no" if it shows something different (another action, another outcome, another person), "unclear" if the picture is
too small or blurry to tell. Answer JSON only:
{{"panels": [{{"supported": "yes" | "no" | "unclear",
  "seen": one sentence: what the picture actually shows (who is where, screen-left/right as in the picture, clothes
          colours and numbers, where the ball is)}} one per panel, in order]}}"""

REPLAN_NOTE = """A check of your plan below against the frames found panels that tell something their pictures do not
show:
{notes}
Your plan was: {plan}
Write the plan again in the same JSON shape. Keep only what the frames show: if the panels followed a player who
does not make the main play, follow the one who does."""

# Captions and sound effects that claim something only sound or a rulebook could show.
UNSEEN = re.compile(r"whistle|foul|tw+e+t|referee'?s? (call|decision)|\bthe call\b|\bcall(ed)?\b|哨|犯规|判罚|裁判判",
                    re.IGNORECASE)

SHEET_PROMPT = (
    "Image 1 is a zoomed picture of the lead of a manga, from a real video. Draw a character reference sheet of this "
    "one person: full-body front view, back view and side view, and a head close-up, side by side on a plain white "
    "background. The lead: {lead}. {age} Keep the real clothes colours from image 1. Image 1 may show other people "
    "too: draw only the lead described here. Give the jersey a number only if that description names one; otherwise "
    "leave the jersey plain. Style: {style}; {palette}.{technique}{photo} No other people. No text or labels anywhere "
    "except that jersey number.")

CHECK_PROMPT = (
    "Image 1 is a picture from a real video, image 2 is a manga panel drawn from it{sheet}. Answer JSON only: "
    '{{"text": true if image 2 shows any letters, words, logos or numbers other than numbers on clothes, '
    '"extra_people": true if image 2 adds a large person who is not in image 1, '
    '"flipped": true if image 2 is a left-right mirror image of image 1, '
    '"colours_ok": true if every person in image 2 wears the same clothes colour as in image 1, '
    '"lead_ok": {lead_q}, '
    '"style_ok": true if image 2 is a hand-drawn manga drawing in {palette}, false if it looks like a photo or uses '
    'the wrong colours, '
    '"fix": one short instruction to fix the problems, or ""}}')
FIX_PROMPT = (
    "Edit the first image, a manga panel. Keep everything else identical: the same drawing style, framing, people, "
    "poses and colours. {fix} {sheet}Remove any letters, logos or words except numbers on clothes. "
    "Output one single illustration.")


def _client():
    from google import genai
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


# ---------- facts and storyboard ----------

def extract_frames(video: Path, out: Path, fps: float, max_frames: int) -> tuple[list[Path], float]:
    """Evenly spaced frames for the storyboard, 768 px wide. Frame k is at k / fps seconds."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("f_*.jpg"):
        old.unlink()
    dur, _, _ = clips.probe(video)
    fps = min(fps, max_frames / max(dur, 1))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps={fps:.3f},scale=768:-2",
                    "-frames:v", str(max_frames), str(out / "f_%03d.jpg")], check=True)
    return sorted(out.glob("f_*.jpg")), fps


def _generate(client, model: str, contents: list, config):
    """One Gemini call, streamed when the client can. A long call that sends nothing while the model thinks gets
    cut by proxies on the way ("Server disconnected without sending a response"); streaming keeps bytes flowing.
    The chunks are merged back into one response with .text, .usage_metadata and .candidates."""
    stream = getattr(client.models, "generate_content_stream", None)
    if stream is None:
        return client.models.generate_content(model=model, contents=contents, config=config)
    from google.genai import types
    if "image" not in model and hasattr(config, "model_copy"):   # thought summaries flow while the model thinks
        config = config.model_copy(update={"thinking_config": types.ThinkingConfig(include_thoughts=True)})
    parts, texts, usage = [], [], None
    for chunk in stream(model=model, contents=contents, config=config):
        usage = getattr(chunk, "usage_metadata", None) or usage
        for cand in getattr(chunk, "candidates", None) or []:
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                parts.append(part)
                if getattr(part, "text", None) and not getattr(part, "thought", False):
                    texts.append(part.text)
    return SimpleNamespace(text="".join(texts), usage_metadata=usage,
                           candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


def _text_call(client, oid: str | None, step: str, contents: list, model: str, price_in: float, price_out: float,
               sleep=time.sleep) -> dict:
    from google.genai import types
    r = clips._retry(lambda: _generate(
        client, model, contents, types.GenerateContentConfig(response_mime_type="application/json")), sleep)
    u = getattr(r, "usage_metadata", None)
    if oid and u:
        tin = u.prompt_token_count or 0
        tout = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        db.add_cost(oid, step, model, tin + tout, tin * price_in + tout * price_out)
    try:
        data = json.loads(r.text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _box(value) -> tuple[float, float, float, float] | None:
    """A [ymin, xmin, ymax, xmax] box on the 0-1000 scale, or None if it isn't one."""
    try:
        y0, x0, y1, x1 = (min(1000.0, max(0.0, float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    return (y0, x0, y1, x1) if y1 - y0 >= 5 and x1 - x0 >= 5 else None


def still(video: Path, t: float, out: Path, overlays: list | None = None) -> Path:
    """The full-resolution frame at t seconds, with broadcast graphics painted out."""
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                        "-frames:v", "1", "-q:v", "2", str(tmp)], check=True)
        if not tmp.exists():         # past the last frame: take the last one
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.2", "-i", str(video), "-frames:v", "1",
                            "-q:v", "2", "-update", "1", str(tmp)], check=True)
        clips.paint_out([{"src": tmp}], overlays or [])
        tmp.replace(out)
    return out


def _crop_rect(size: tuple[int, int], box, ratio: float, min_frac: float = 0.28,
               pad: float = 0.12) -> tuple[int, int, int, int]:
    """The rectangle (pixels) around box (0-1000 scale), padded, at least min_frac of the frame wide, grown to the
    width/height ratio and kept inside the frame. A missing box gives the whole frame at that ratio."""
    W, H = size
    b = _box(box)
    if b:
        y0, x0, y1, x1 = b[0] * H / 1000, b[1] * W / 1000, b[2] * H / 1000, b[3] * W / 1000
        w, h = (x1 - x0) * (1 + 2 * pad), (y1 - y0) * (1 + 2 * pad)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    else:
        w, h, cx, cy = W, H, W / 2, H / 2
    w = max(w, min_frac * W)
    if w / h < ratio:
        w = h * ratio
    else:
        h = w / ratio
    s = min(1.0, W / w, H / h)          # never larger than the frame
    w, h = w * s, h * s
    left = min(max(0.0, cx - w / 2), W - w)
    top = min(max(0.0, cy - h / 2), H - h)
    return int(left), int(top), int(left + w), int(top + h)


def crop(img: Image.Image, box, ratio: float, min_frac: float = 0.28, pad: float = 0.12) -> Image.Image:
    return img.crop(_crop_rect(img.size, box, ratio, min_frac, pad))


HOOP_FPS = 8
HOOP_NOTE = """
After the zoom frames come {n} "Hoop frames", {fps} a second, showing only the basket the play attacks, close up.
Decide whether a shot goes in ONLY from these: it goes in if the ball is seen inside the rim or dropping through the
net; a ball seen bouncing away from the rim without going through is a miss. Your ending must agree with them."""


def hoop_frames(video: Path, out: Path, hoop) -> list[tuple[float, Image.Image]]:
    """Close-ups of the basket the play attacks, several a second. In a wide gym shot the rim is a few dozen pixels
    in the zoom frames, and the planning call guessed made or missed differently from run to run; this close-up
    shows the ball inside the rim or not."""
    if not hoop:
        return []
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("h_*.jpg"):
        old.unlink()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", f"fps={HOOP_FPS}",
                    "-q:v", "3", str(out / "h_%03d.jpg")], check=True)
    y0, x0, y1, x1 = hoop
    cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
    half_h, half_w = max(y1 - y0, 120) * 0.9, max(x1 - x0, 80) * 0.9          # the rim plus the air around it
    box = [max(0, cy - half_h), max(0, cx - half_w), min(1000, cy + half_h), min(1000, cx + half_w)]
    frames = []
    for k, path in enumerate(sorted(out.glob("h_*.jpg"))):
        img = Image.open(path).convert("RGB")
        W, H = img.size
        frames.append((round(k / HOOP_FPS, 3), img.crop((int(box[1] * W / 1000), int(box[0] * H / 1000),
                                                         int(box[3] * W / 1000), int(box[2] * H / 1000)))))
    return frames


def storyboard(client, video: Path, work: Path, theme: str, lang: str, oid: str | None = None,
               sleep=time.sleep) -> dict:
    """Where first, from whole frames (the play's position over time, the broadcast graphics, the teams); then who
    does what, from zoomed frames that follow the play, and panels planned only from those facts. Players in a wide
    gym shot are 40 px tall in a whole frame: from those alone the model named the wrong shooter and ending."""
    frames, fps = extract_frames(video, work / "frames", settings.STORY_FPS, settings.STORY_MAX_FRAMES)
    dur, _, _ = clips.probe(video)
    facts_file = work / "facts.json"
    if facts_file.exists():
        facts = json.loads(facts_file.read_text(encoding="utf-8"))
    else:
        facts = _text_call(client, oid, "storyboard (facts)",
                           [FACTS_PROMPT.format(n=len(frames), fps=round(fps, 3), dur=dur)]
                           + [_part(f) for f in frames],
                           settings.GEMINI_TEXT_MODEL, STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT, sleep)
        facts_file.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
    overlays = facts.get("overlays") or []
    rects, zooms = [], []
    for k in range(len(frames)):
        t = _clamp_t(k / fps, dur)
        full = Image.open(still(video, t, work / "src" / f"t_{t:.2f}.jpg", overlays)).convert("RGB")
        rect = follow_box(facts.get("events") or [], t, full.size)
        rects.append(rect)
        zooms += [f"Zoom frame {k} (t = {t:.2f} s):", _part(full.crop(rect), 768)]
    hoops = hoop_frames(video, work / "hoop", _box(facts.get("hoop")))
    hoop_note = HOOP_NOTE.format(n=len(hoops), fps=HOOP_FPS) if hoops else ""
    for t, frame in hoops:
        zooms += [f"Hoop frame (t = {t:.2f} s):", _part(frame, 384)]
    asked = [PLAN_PROMPT.format(dur=dur, n=len(frames), fps=round(fps, 3), hoop_note=hoop_note,
                                tone=THEME_TONE.get(theme, THEME_TONE["sports"]), lang=lang,
                                context=json.dumps(_context(facts), ensure_ascii=False))] + zooms
    raw = _text_call(client, oid, "storyboard (panels)", asked, settings.GEMINI_TEXT_MODEL,
                     STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT, sleep)
    plan = normalise_plan(raw, facts, dur, rects=rects, fps=fps, size=full.size)
    verdicts = panel_verdicts(client, plan, video, work / "verify.json", oid, sleep)
    wrong = [(k, p, v) for k, (p, v) in enumerate(zip(_panels(plan), verdicts)) if _rejected(v)]
    if len(wrong) >= 2:
        # Several panels tell something their own pictures don't show (e.g. it followed the wrong player):
        # plan once more, told exactly what the pictures showed.
        notes = "\n".join(f"- panel at t = {p['t']:.2f} s claimed: {p.get('description', '')} The picture shows: "
                          f"{v.get('seen')}" for _, p, v in wrong)
        raw = _text_call(client, oid, "storyboard (replan)",
                         asked + [REPLAN_NOTE.format(notes=notes, plan=json.dumps(raw, ensure_ascii=False))],
                         settings.GEMINI_TEXT_MODEL, STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT, sleep)
        plan = normalise_plan(raw, facts, dur, rects=rects, fps=fps, size=full.size)
        verdicts = panel_verdicts(client, plan, video, work / "verify2.json", oid, sleep)
    return apply_verdicts(plan, verdicts)


def _panels(plan: dict) -> list[dict]:
    return [p for page in plan["pages"] for p in page["panels"]]


def _rejected(verdict: dict) -> bool:
    return str(verdict.get("supported", "")).lower() == "no" and bool(str(verdict.get("seen") or "").strip())


def verify_plan(client, plan: dict, video: Path, work: Path, oid: str | None = None, sleep=time.sleep) -> dict:
    return apply_verdicts(plan, panel_verdicts(client, plan, video, work / "verify.json", oid, sleep))


def panel_verdicts(client, plan: dict, video: Path, out: Path, oid: str | None = None, sleep=time.sleep) -> list:
    """A second look at every panel's own picture before anything is drawn. The planning call sometimes tells a
    story the frames don't show (a made basket became "Rolls off!", "Rebound secured!", "The call!")."""
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    work = out.parent
    overlays = plan.get("overlays") or []
    contents = [VERIFY_PROMPT]
    for k, panel in enumerate(_panels(plan)):
        full = Image.open(still(video, panel["t"], work / "src" / f"t_{panel['t']:.2f}.jpg", overlays)).convert("RGB")
        contents += [f"Panel {k}:", _part(crop(full, panel.get("box"), 16 / 9, 0.45, 0.25), 768),
                     f"Claim {k}: {panel.get('description', '')} Caption: {panel.get('caption', '')}"]
    answer = _text_call(client, oid, "storyboard (verify)", contents, settings.GEMINI_TEXT_MODEL,
                        STORYBOARD_PRICE_IN, STORYBOARD_PRICE_OUT, sleep)
    verdicts = [v if isinstance(v, dict) else {} for v in (answer.get("panels") or [])]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdicts, ensure_ascii=False), encoding="utf-8")
    return verdicts


def apply_verdicts(plan: dict, verdicts: list) -> dict:
    """A panel its picture contradicts is drawn as what the picture shows, with no caption. Captions about
    whistles, fouls or calls are dropped: sound and rulings are not in the frames."""
    panels = _panels(plan)
    for panel, v in zip(panels, verdicts):
        if _rejected(v):
            panel.update(description=str(v["seen"]).strip(), caption="", sfx="", verified="replaced")
    for panel in panels:
        for field in ("caption", "sfx"):
            if UNSEEN.search(str(panel.get(field) or "")):
                panel[field] = ""
    return plan


def _context(facts: dict) -> dict:
    """What the zoom pass may take from the whole-frame pass. Not who did what: from 40 px players the first pass
    named the wrong shooter, and given its events the second pass repeated them instead of looking."""
    return {k: facts.get(k) for k in ("on_screen_text", "age", "groups", "others") if facts.get(k)}


def follow_box(events: list, t: float, size: tuple[int, int], min_frac: float = 0.45) -> tuple[int, int, int, int]:
    """A 16:9 crop rectangle (pixels) that follows the play: the event boxes, interpolated in time."""
    W, H = size
    marks = sorted(((float(e["t"]), b) for e in events if isinstance(e, dict)
                    for b in [_box(e.get("box"))] if b and _is_number(e.get("t"))), key=lambda m: m[0])
    if not marks:
        box = None
    elif t <= marks[0][0]:
        box = marks[0][1]
    elif t >= marks[-1][0]:
        box = marks[-1][1]
    else:
        i = max(i for i, m in enumerate(marks) if m[0] <= t)
        (t0, b0), (t1, b1) = marks[i], marks[i + 1]
        a = 0 if t1 == t0 else (t - t0) / (t1 - t0)
        box = tuple(v0 + a * (v1 - v0) for v0, v1 in zip(b0, b1))
    return _crop_rect(size, box, 16 / 9, min_frac, 0.25)


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _jpeg(source, longest: int | None = None) -> bytes:
    """JPEG bytes of a path or an image. The SDK sends a cropped or converted PIL image as PNG, which made
    requests with a dozen pictures several times larger (and slow enough to fail at the gateway)."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        if longest is None and path.suffix.lower() in (".jpg", ".jpeg"):
            return path.read_bytes()
        with Image.open(path) as img:
            return _jpeg(img.convert("RGB"), longest)
    img = source.convert("RGB")
    if longest:
        img = img.copy()
        img.thumbnail((longest, longest))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _part(source, longest: int | None = None):
    from google.genai import types
    return types.Part.from_bytes(data=_jpeg(source, longest), mime_type="image/jpeg")


def _clamp_t(t, dur: float) -> float:
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = 0.0
    return min(max(0.0, t), max(0.0, dur - 0.05))


def normalise_plan(plan: dict, facts: dict, dur: float, rects: list | None = None, fps: float = 2.0,
                   size: tuple[int, int] | None = None) -> dict:
    """Keep only pages with 1-4 well-formed panels, so the layout and the panel prompts can rely on the fields.
    Panel boxes come on the zoom frame nearest their time; they are turned into boxes on the full frame."""
    pages = []
    for page in plan.get("pages") or []:
        panels = [p for p in (page or {}).get("panels") or [] if isinstance(p, dict)][:4]
        for p in panels:
            if "t" not in p and "frame" in p:            # plans saved before 2026-11-09 used 2 fps frame numbers
                p["t"] = float(p.get("frame") or 0) / 2
            p["t"] = _clamp_t(p.get("t"), dur)
            p["shot"] = p.get("shot") if p.get("shot") in ("wide", "medium", "close-up") else "medium"
            p["description"] = str(p.get("description") or "")
            if rects and size:
                p["box"] = _zoom_to_full(p.get("box"), rects[min(len(rects) - 1, round(p["t"] * fps))], size)
        if panels:
            pages.append({"panels": panels})
    if not pages:
        raise RuntimeError("The storyboard came back without panels")
    cast = plan.get("cast") if isinstance(plan.get("cast"), dict) else {}
    lead = facts.get("lead") if isinstance(facts.get("lead"), dict) else {}
    cast.setdefault("lead", lead.get("look") or "")
    cast.setdefault("age", facts.get("age") or "")
    cast.setdefault("groups", [g.get("look", "") for g in facts.get("groups") or [] if isinstance(g, dict)])
    return {"title": plan.get("title") or "", "cast": cast, "pages": pages,
            "facts": plan.get("facts") or {}, "overlays": facts.get("overlays") or []}


def _zoom_to_full(box, rect, size) -> list[float]:
    """A box on a zoom frame (0-1000) as a box on the full frame (0-1000). No box: the whole zoom frame."""
    W, H = size
    left, top, right, bottom = rect
    b = _box(box) or (0.0, 0.0, 1000.0, 1000.0)
    zw, zh = right - left, bottom - top
    return [round((top + b[0] * zh / 1000) * 1000 / H, 1), round((left + b[1] * zw / 1000) * 1000 / W, 1),
            round((top + b[2] * zh / 1000) * 1000 / H, 1), round((left + b[3] * zw / 1000) * 1000 / W, 1)]


# ---------- panel prompts ----------

def _palette(style: str) -> str:
    return PALETTE.get(style, FULL_COLOUR)


def _age_rule(cast: dict) -> str:
    age = str(cast.get("age") or "").lower()
    if age in ("children", "teenagers", "mixed"):
        who = "children" if age == "children" else "young teenagers (about 12 to 16)"
        return (f"The players are {who}: keep their real slim, small builds and boyish or girlish young faces; no adult "
                "muscles, jawlines or stubble. Adults (referees, coaches) stay adults.")
    return "Everyone keeps their real age and build as in the video."


def _cast_rule(cast: dict) -> str:
    groups = [str(g) for g in cast.get("groups") or [] if str(g).strip()]
    others = [str(o) for o in cast.get("others") or [] if str(o).strip()]
    parts = []
    if groups:
        parts.append("Teams and clothes: " + "; ".join(groups) + ".")
    if others:
        parts.append("Others: " + "; ".join(others) + ".")
    parts.append("Keep every jersey colour and number exactly as in image 1.")
    return " ".join(parts)


def panel_prompt(panel: dict, style: str, aspect: str, cast: dict | None = None, sheet: bool = False,
                 technique: bool = False) -> str:
    cast = cast or {}
    n = 2
    lines = [f"Image 1 is a picture from a real video: it is the ONLY source of content. Redraw it as ONE single manga "
             f"panel ({panel['shot']} shot) in a {STYLE_PROMPTS[style]}; {_palette(style)}.",
             "Keep the composition, camera angle, framing and every person's position, pose, direction and size exactly "
             "as in image 1, and the ball exactly where it is. NOT flipped horizontally. No extra or missing people.",
             _cast_rule(cast), _age_rule(cast)]
    if sheet:
        lines.append(f"Image {n} is the character sheet of the lead ({cast.get('lead') or 'the main player'}): wherever "
                     "the lead appears, draw the lead exactly like the sheet (face, hair, clothes colour, number). Other "
                     f"players keep their own looks and numbers from image 1. Copy nothing else from image {n}.")
        n += 1
    if technique:
        lines.append(f"Image {n} is a small crop that shows ONLY the drawing technique to use; copy nothing from it "
                     "(no people, no place, no layout).")
    lines += [f"In this panel: {panel['description']} If this description and image 1 disagree, image 1 wins.",
              "Remove any broadcast score bar and on-screen graphics. Draw banners, signs and walls as plain shapes "
              "with no writing. No text, letters, logos or numbers anywhere except numbers on clothes; no speech "
              "bubbles, no sound effects.",
              f"Aspect ratio {aspect}. Output one single illustration."]
    return " ".join(lines)


def _image_config(aspect: str, size: str | None = None):
    from google.genai import types
    shape = types.ImageConfig(aspect_ratio=aspect, image_size=size) if size else types.ImageConfig(aspect_ratio=aspect)
    return types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=shape)


def _image_from(r) -> Image.Image:
    for cand in getattr(r, "candidates", None) or []:
        for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return Image.open(BytesIO(part.inline_data.data)).convert("RGB")
    raise RuntimeError("Image model returned no image")


def _image_call(client, contents: list, aspect: str, sleep=time.sleep, size: str | None = None) -> Image.Image:
    last = None
    for _ in range(2):                         # the model sometimes answers with text only
        r = clips._retry(lambda: _generate(client, settings.GEMINI_IMAGE_MODEL, contents,
                                           _image_config(aspect, size)), sleep)
        try:
            return _image_from(r)
        except RuntimeError as exc:
            last = exc
    raise last


def panel_contents(job: dict, style: str, cast: dict, sheet: Path | None) -> list:
    """Images first (numbered in the prompt in this order), then the prompt."""
    if job.get("prompt"):       # a directed panel brings its own pictures and words (app/directed.py)
        return [_part(p, DIRECTED_PX) for p in job.get("images") or []] + [job["prompt"]]
    ref = _style_ref(style)
    images = [_part(job["src"])] + ([_part(sheet, 1024)] if sheet else []) + ([_part(ref)] if ref else [])
    return images + [panel_prompt(job["panel"], style, job["aspect"], cast, bool(sheet), bool(ref))]


def _style_ref(style: str) -> Path | None:
    ref = STYLE_REFS.get(style)
    path = ref() if ref else None
    return path if path and Path(path).exists() else None


def draw_panel(client, job: dict, style: str, cast: dict, sheet: Path | None, sleep=time.sleep) -> Image.Image:
    return _image_call(client, panel_contents(job, style, cast, sheet), job["aspect"], sleep, job.get("size"))


def draw_sheet(client, order: dict, plan: dict, jobs: list[dict], photo: Path | None, work: Path,
               sleep=time.sleep) -> Path | None:
    """One character sheet of the lead per order, from the tightest panel source that shows the lead."""
    out = work / "sheet.png"
    if out.exists():
        return out
    if not jobs:
        return None
    rank = {"close-up": 0, "medium": 1, "wide": 2}
    best = min(jobs, key=lambda j: rank.get(j["panel"].get("shot"), 3))
    style, ref = order["style"], _style_ref(order["style"])
    images = [_part(best["src"])] + ([_part(ref)] if ref else []) + ([_part(photo, 1024)] if photo else [])
    prompt = SHEET_PROMPT.format(
        lead=plan.get("cast", {}).get("lead") or "the main player", age=_age_rule(plan.get("cast", {})),
        style=STYLE_PROMPTS[style], palette=_palette(style),
        technique=" Image 2 shows ONLY the drawing technique to use; copy nothing else from it." if ref else "",
        photo=f" {PHOTO_NOTE}" if photo else "")
    art = _image_call(client, images + [prompt], "16:9", sleep)
    _save(art, out)
    db.add_cost(order["id"], "character sheet", settings.GEMINI_IMAGE_MODEL, 1, PANEL_PRICE["standard"])
    return out


# ---------- lettering and layout ----------

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


def nearest_aspect(ratio: float) -> str:
    return min(ASPECTS, key=lambda a: abs(math.log(ratio) - math.log(_ratio(a))))


def _ratio(aspect: str) -> float:
    w, h = aspect.split(":")
    return float(w) / float(h)


# ---------- the order ----------

def run(order: dict, progress=lambda p: None, client=None, sleep=time.sleep, now=time.time) -> Path:
    from . import directed
    if directed.enabled():      # ANTHROPIC_API_KEY is set: Claude directs and reviews, Gemini draws
        return directed.run(order, progress, client, sleep, now)
    work = settings.DATA_DIR / "jobs" / order["id"]
    work.mkdir(parents=True, exist_ok=True)
    src = Path(order["video"])
    client = client or _client()
    lang = "Simplified Chinese" if order.get("lang") == "zh" else "English"
    plan_file = work / "plan.json"
    if plan_file.exists():      # a restarted order keeps its storyboard, so saved panels and batch results still fit
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    else:
        plan = storyboard(client, src, work, order["theme"], lang, order["id"], sleep)
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    progress(15)
    jobs = panel_jobs(plan, src, work)
    photo = Path(order["photo"]) if order.get("photo") else None
    sheet = draw_sheet(client, order, plan, jobs, photo, work, sleep)
    progress(20)
    if order.get("api_style") == "batch":
        draw_batch(client, order, jobs, plan, sheet, work, progress, sleep, now)
    draw_standard(client, order, jobs, plan, sheet, work, progress, sleep)   # after a batch: only what it missed
    check_and_fix(client, order, jobs, plan, sheet, work, progress, sleep)
    pages = []
    for pi, page in enumerate(plan["pages"]):
        page_img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        d = ImageDraw.Draw(page_img)
        for job in (j for j in jobs if j["page"] == pi):
            x, y, w, h = job["box"]
            art = Image.open(work / f"{job['key']}.png").convert("RGB")
            art = letter(_cover(art, w, h), job["panel"].get("caption", ""), job["panel"].get("sfx", ""))
            page_img.paste(art, (x, y))
            d.rectangle([x, y, x + w, y + h], outline="black", width=6)
        page_img.save(work / f"page_{pi + 1}.jpg", quality=92)
        pages.append(page_img)
    progress(97)
    pdf = work / "manga.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=200)
    return pdf


def panel_jobs(plan: dict, video: Path, work: Path) -> list[dict]:
    """One entry per panel: its key (also the PNG name), page, box on the page, aspect, and its source picture:
    the full-resolution frame at the panel's time, graphics painted out, cropped to the panel's box and shape."""
    jobs = []
    for pi, page in enumerate(plan["pages"]):
        for panel, box in zip(page["panels"], layout(len(page["panels"]))):
            key = f"P{pi + 1}-{len(jobs) + 1}"
            aspect = nearest_aspect(box[2] / box[3])
            source = work / "src" / f"{key}.jpg"
            if not source.exists():
                t = float(panel.get("t") or 0)
                full = Image.open(still(video, t, work / "src" / f"t_{t:.2f}.jpg", plan.get("overlays"))).convert("RGB")
                pic = crop(full, panel.get("box"), _ratio(aspect),
                           min_frac={"close-up": 0.3, "medium": 0.45}.get(panel.get("shot"), 0.7))
                if pic.width < 1024:
                    pic = pic.resize((1024, int(1024 * pic.height / pic.width)), Image.LANCZOS)
                pic.save(source, quality=92)
            jobs.append({"key": key, "page": pi, "panel": panel, "box": box, "src": source, "aspect": aspect})
    return jobs


def _save(img: Image.Image, out: Path) -> None:
    tmp = out.with_suffix(".tmp.png")       # never leave half a PNG behind for a restarted order to reuse
    img.save(tmp)
    tmp.replace(out)


def draw_standard(client, order: dict, jobs: list[dict], plan: dict, sheet: Path | None, work: Path, progress,
                  sleep=time.sleep) -> None:
    """Interactive calls, one panel at a time. Panels already on disk are kept."""
    todo = [j for j in jobs if not (work / f"{j['key']}.png").exists()]
    for n, job in enumerate(todo, 1):
        art = draw_panel(client, job, order["style"], plan.get("cast") or {}, sheet, sleep)
        _save(art, work / f"{job['key']}.png")
        db.add_cost(order["id"], "book panel", settings.GEMINI_IMAGE_MODEL, 1, PANEL_PRICE["standard"])
        progress(20 + 65 * (len(jobs) - len(todo) + n) / len(jobs))


# ---------- check and fix ----------

def problems(verdict: dict) -> int:
    """A panel that isn't manga counts most; a mirrored one or one with an extra person next."""
    return (3 * (verdict.get("style_ok") is False) + 2 * bool(verdict.get("flipped"))
            + 2 * bool(verdict.get("extra_people")) + bool(verdict.get("text"))
            + (verdict.get("colours_ok") is False) + (verdict.get("lead_ok") is False))


def check_panel(client, order: dict, job: dict, art: Path, sheet: Path | None, sleep=time.sleep) -> dict:
    images = [_part(job["src"], 768), _part(art, 768)] + ([_part(sheet, 768)] if sheet else [])
    prompt = CHECK_PROMPT.format(
        sheet=", image 3 is the character sheet of the lead" if sheet else "",
        lead_q=("true if the lead in image 2 matches the sheet in image 3 (clothes colour, number, hair), or if the "
                "lead is not in image 2" if sheet else "true"),
        palette=_palette(order["style"]))
    return _text_call(client, order["id"], "panel check", images + [prompt], settings.VIDEO_CHECK_MODEL,
                      CHECK_PRICE_IN, CHECK_PRICE_OUT, sleep)


def check_and_fix(client, order: dict, jobs: list[dict], plan: dict, sheet: Path | None, work: Path, progress,
                  sleep=time.sleep) -> None:
    """Check every panel once; give at most BOOK_MAX_FIXES of the worst ones one fix each."""
    log_file = work / "checks.json"
    log = json.loads(log_file.read_text()) if log_file.exists() else {}
    for n, job in enumerate(jobs, 1):
        if job["key"] not in log:
            log[job["key"]] = {"verdict": check_panel(client, order, job, work / f"{job['key']}.png", sheet, sleep)}
            log_file.write_text(json.dumps(log))
        progress(85 + 5 * n / len(jobs))
    fixes = sum(1 for entry in log.values() if "fixed" in entry)
    flagged = sorted((j for j in jobs if problems(log[j["key"]]["verdict"]) and "fixed" not in log[j["key"]]),
                     key=lambda j: -problems(log[j["key"]]["verdict"]))
    for job in flagged:
        if fixes >= settings.BOOK_MAX_FIXES:
            break
        before = log[job["key"]]["verdict"]
        art = work / f"{job['key']}.png"
        if before.get("flipped") or before.get("extra_people"):
            # An edit keeps the wrong composition; draw the panel again from its source instead.
            fixed = draw_panel(client, job, order["style"], plan.get("cast") or {}, sheet, sleep)
            step = "panel redraw"
        else:
            images = [_part(art)] + ([_part(sheet, 1024)] if sheet else [])
            fixed = _image_call(client, images + [FIX_PROMPT.format(
                fix=before.get("fix") or "", sheet="The second image is the lead's character sheet: the lead must "
                "match it. " if sheet else "")], job["aspect"], sleep)
            step = "panel fix"
        db.add_cost(order["id"], step, settings.GEMINI_IMAGE_MODEL, 1, PANEL_PRICE["standard"])
        fixes += 1
        candidate = work / f"{job['key']}.fix.png"
        _save(fixed, candidate)
        after = check_panel(client, order, job, candidate, sheet, sleep)
        kept = after.get("style_ok") is not False and problems(after) < problems(before)
        if kept:
            candidate.replace(art)
        else:
            candidate.unlink(missing_ok=True)
        log[job["key"]].update(fixed=kept, after=after)
        log_file.write_text(json.dumps(log))
    progress(95)


# ---------- Gemini Batch API ----------

def draw_batch(client, order: dict, jobs: list[dict], plan: dict, sheet: Path | None, work: Path, progress,
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
        state = _submit_batch(client, order, todo, plan, sheet, work, now)
        state_file.write_text(json.dumps(state))
    progress(25)
    job = _wait_batch(client, state, sleep, now)
    return _collect_batch(client, job, order, todo, work)


def _jpeg_b64(path, longest: int | None = None) -> str:
    return base64.b64encode(_jpeg(path, longest)).decode()


def _submit_batch(client, order: dict, todo: list[dict], plan: dict, sheet: Path | None, work: Path, now) -> dict:
    from google.genai import types
    # The sheet and the style reference go into every request, so shrink them to keep the request file small.
    ref = _style_ref(order["style"])
    shared = [_jpeg_b64(p, 1024) for p in (sheet, ref) if p]
    requests = work / "batch_requests.jsonl"
    with requests.open("w", encoding="utf-8") as f:
        for job in todo:
            if job.get("prompt"):       # a directed panel: its own pictures and words, as in panel_contents
                images, words = [_jpeg_b64(p, DIRECTED_PX) for p in job.get("images") or []], job["prompt"]
            else:
                images = [_jpeg_b64(job["src"])] + shared
                words = panel_prompt(job["panel"], order["style"], job["aspect"], plan.get("cast") or {},
                                     bool(sheet), bool(ref))
            parts = [{"inlineData": {"mimeType": "image/jpeg", "data": data}} for data in images]
            parts.append({"text": words})
            shape = {"aspectRatio": job["aspect"], **({"imageSize": job["size"]} if job.get("size") else {})}
            f.write(json.dumps({"key": job["key"], "request": {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": shape}}}) + "\n")
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
