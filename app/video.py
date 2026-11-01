"""Clip -> moving manga video (the "all" product).

1. Sample one source frame every VIDEO_KF_INTERVAL seconds.
2. One cheap call on the middle frame finds broadcast graphics (painted out of every frame)
   and the team jersey colours (turned into a colour rule for every prompt).
3. Draw keyframes in order with the image model. Each keyframe also gets the previous one,
   so the gym, crowd and players stay consistent.
4. A cheap model checks each keyframe (text, extra people, wrong colours); a flagged one gets
   one image-edit fix before the next keyframe is drawn from it.
5. Hold each keyframe until the next (an animatic) and send it to the motion model in
   overlapping pieces of at most 6.6 s; join the pieces with 1 s crossfades.

Every paid call is cached by a hash of its exact inputs, so a retried order does not pay twice,
and is written to the order's cost ledger. The order stops before any call that would take it
past VIDEO_ORDER_BUDGET_USD.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image

from . import db, settings

# Live list prices, Oct 2026 (USD). Keep in step with the Gemini pricing page.
IMAGE_PRICE = {"512": 0.045, "1K": 0.067}
CHECK_PRICE_IN, CHECK_PRICE_OUT = 0.10 / 1e6, 0.40 / 1e6
MOTION_PRICE_OUT = 17.50 / 1e6
MOTION_TOKENS_PER_SECOND = 6000          # measured: about 38.6k output tokens per 6.5 s piece

PIECE_MAX, OVERLAP = 6.6, 1.0
FPS = 24

STYLE_PROMPT = (
    "Image 1 is a frame from a real basketball video: it is the ONLY source of content. "
    "Image 2 is a small crop that shows ONLY the drawing technique to use; copy nothing from image 2 "
    "(no people, no gym, no layout). Redraw image 1 as one single black-and-white ink manga illustration "
    "using that technique (clean ink lines, screentone shading, hand-drawn crowd). Keep the composition, "
    "camera angle, framing, gym, crowd and every player's position and pose exactly as in image 1, NOT flipped "
    "horizontally, no zoom, no extra or missing people. {rule} Keep the ball exactly where it is. Remove any "
    "broadcast score bar and on-screen graphics. Draw banners and signs as plain blank shapes. No text, letters "
    "or numbers anywhere except jersey numbers. Output one single illustration.")
CHAIN_PROMPT = (
    " Image 3 is the PREVIOUS drawing of this same shot: keep the gym, crowd, banners, floor markings, line "
    "weight, shading and every player's look identical to image 3, and only change what moved or what the "
    "camera reveals in image 1.")
INSPECT_PROMPT = (
    "This is a frame from a basketball video. Answer JSON only: "
    '{"overlays": [[ymin, xmin, ymax, xmax], ...] boxes (0-1000 scale) around broadcast graphics drawn on top '
    "of the picture (score bar, channel logo, captions, replay badges); [] if none, "
    '"teams": [colour of team A jerseys, colour of team B jerseys] as short plain colour names}')
CHECK_PROMPT = (
    "Image 1 is a real video frame, image 2 is a manga redraw of it. Answer JSON only: "
    '{"text": true if image 2 shows any letters, words, logos or numbers other than numbers on player jerseys, '
    '"extra_people": true if image 2 adds a large foreground person or crowd that is not in image 1, '
    '"colours_ok": true if every player in image 2 wears the same jersey colour as in image 1, '
    '"fix": one short instruction to fix the problems, or ""}')
FIX_PROMPT = (
    "Edit image 1 (keep everything else identical, same style and framing; image 2 is the real frame it was "
    "drawn from): {fix} Remove any letters, logos or words except jersey numbers.")
MOTION_PROMPT = (
    "This video is a rough animatic of manga keyframes drawn from a real basketball play. Each drawing is held "
    "still only as a placeholder: never freeze. Animate continuous movement through every drawing: players keep "
    "running and dribbling, the ball keeps moving, and the camera pans smoothly from one drawing's framing to the "
    "next. Keep the same black-and-white ink manga style, the same players in the same places at each drawing, "
    "and the same ball path. No cuts or new shots. {rule} Do not add any text, banners, signs, captions or new "
    "people. No music, natural gym sound only.")


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Writes each paid call to the order's cost ledger and refuses calls past the budget."""

    def __init__(self, oid: str, budget: float):
        self.oid, self.budget = oid, budget

    def reserve(self, step: str, estimate: float) -> None:
        spent = db.order_cost_total(self.oid)
        if spent + estimate > self.budget:
            raise BudgetExceeded(f"{step}: ${spent:.2f} spent + ${estimate:.2f} would pass the "
                                 f"${self.budget:.2f} order budget")

    def add(self, step: str, model: str, units: float, usd: float, cached: bool = False) -> None:
        db.add_cost(self.oid, step, model, units, usd, cached)


class Cache:
    """Content-addressed store: the same inputs to the same model return the stored output."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(*parts) -> str:
        h = hashlib.sha256()
        for part in parts:
            if isinstance(part, Path):
                part = part.read_bytes()
            elif isinstance(part, Image.Image):
                buf = io.BytesIO()
                part.save(buf, "PNG")
                part = buf.getvalue()
            elif not isinstance(part, bytes):
                part = json.dumps(part, sort_keys=True).encode()
            h.update(hashlib.sha256(part).digest())
        return h.hexdigest()

    def path(self, key: str, ext: str) -> Path:
        return self.root / key[:2] / f"{key}.{ext}"

    def get(self, key: str, ext: str) -> Path | None:
        p = self.path(key, ext)
        return p if p.exists() else None

    def put(self, key: str, ext: str, src: Path) -> Path:
        p = self.path(key, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(p)
        return p


def _retry(fn, sleep, tries=4):
    """Gemini returns 429 when the account's spend rate is hit and 5xx on proxy timeouts."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # the SDK raises several error classes; match on the message
            text = str(exc)
            transient = any(code in text for code in ("429", "RESOURCE_EXHAUSTED", "500", "502", "503", "504",
                                                      "UNAVAILABLE", "DEADLINE"))
            if not transient or attempt == tries - 1:
                raise
            sleep(min(300, 30 * 2 ** attempt))


# ---------- frames ----------

def probe(video: Path) -> tuple[float, int, int]:
    out = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(video)]))
    stream = out["streams"][0]
    return float(out["format"]["duration"]), int(stream["width"]), int(stream["height"])


def sample_frames(video: Path, out: Path, duration: float, interval: float, size: tuple[int, int]) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    items, k = [], 0
    while (t := interval / 2 + k * interval) < duration:
        dst = out / f"src_{k:03d}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={size[0]}:{size[1]}", str(dst)], check=True)
        if dst.exists():
            items.append({"t": t, "src": dst})
        k += 1
    if not items:
        raise RuntimeError("Could not read frames from the clip")
    return items


def paint_out(frames: list[dict], boxes: list) -> None:
    """Inpaint broadcast graphics (assumed fixed on screen) out of every source frame."""
    if not boxes:
        return
    import cv2
    import numpy as np
    first = cv2.imread(str(frames[0]["src"]))
    h, w = first.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for box in boxes:
        try:
            y0, x0, y1, x1 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        if (y1 - y0) * (x1 - x0) > 0.25 * 1000 * 1000:   # never paint out a quarter of the picture
            continue
        pad = 6
        mask[max(0, int(y0 * h / 1000) - pad):min(h, int(y1 * h / 1000) + pad),
             max(0, int(x0 * w / 1000) - pad):min(w, int(x1 * w / 1000) + pad)] = 255
    for f in frames:
        img = cv2.imread(str(f["src"]))
        cv2.imwrite(str(f["src"]), cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA))


# ---------- model calls ----------

def _json_call(client, ledger, cache, step, images, prompt, sleep) -> dict:
    from google.genai import types
    model = settings.VIDEO_CHECK_MODEL
    key = cache.key(model, prompt, *images)
    hit = cache.get(key, "json")
    if hit:
        ledger.add(step, model, 0, 0, cached=True)
        return json.loads(hit.read_text())
    ledger.reserve(step, 0.002)
    r = _retry(lambda: client.models.generate_content(
        model=model, contents=[*images, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json")), sleep)
    u = r.usage_metadata
    tin, tout = (u.prompt_token_count or 0) if u else 0, (u.candidates_token_count or 0) if u else 0
    ledger.add(step, model, tin + tout, tin * CHECK_PRICE_IN + tout * CHECK_PRICE_OUT)
    try:
        data = json.loads(r.text)
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    tmp = cache.root / f"tmp-{key}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data))
    cache.put(key, "json", tmp)
    tmp.unlink(missing_ok=True)
    return data


def _image_call(client, ledger, cache, step, contents, aspect, out: Path, sleep) -> Path:
    from google.genai import types
    model, size = settings.VIDEO_IMAGE_MODEL, settings.VIDEO_KF_SIZE
    key = cache.key(model, size, aspect, *contents)
    hit = cache.get(key, "png")
    if hit:
        ledger.add(step, model, 1, 0, cached=True)
        shutil.copyfile(hit, out)
        return out
    price = IMAGE_PRICE.get(size, IMAGE_PRICE["1K"])
    for attempt in range(2):      # the model sometimes answers with text only
        ledger.reserve(step, price)
        r = _retry(lambda: client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                               image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size))),
            sleep)
        ledger.add(step, model, 1, price)
        parts = (r.candidates[0].content.parts or []) if r.candidates and r.candidates[0].content else []
        for part in parts:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                Image.open(io.BytesIO(part.inline_data.data)).convert("RGB").save(out)
                cache.put(key, "png", out)
                return out
    raise RuntimeError(f"{step}: image model returned no image")


def team_rule(teams) -> str:
    teams = [str(t).strip() for t in (teams or []) if str(t).strip()][:2]
    if not teams:
        return "Keep every jersey colour exactly as in image 1; everything else is black-and-white ink."
    names = " and ".join(f"the team in {t} jerseys stays {t}" for t in teams)
    return f"Keep the jersey colours exactly as in image 1: {names}. Only the jerseys keep colour; everything else is black-and-white ink."


def draw_keyframes(client, ledger, cache, frames, style: Path, rule: str, aspect: str, out: Path,
                   sleep, progress) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    style_img = Image.open(style).convert("RGB")
    prev, keyframes = None, []
    for i, f in enumerate(frames):
        src = Image.open(f["src"]).convert("RGB")
        dst = out / f"kf_{i:03d}.png"
        contents = [src, style_img] + ([Image.open(prev).convert("RGB")] if prev else [])
        contents.append(STYLE_PROMPT.format(rule=rule) + (CHAIN_PROMPT if prev else ""))
        _image_call(client, ledger, cache, "keyframe", contents, aspect, dst, sleep)
        verdict = _json_call(client, ledger, cache, "check", [src, Image.open(dst).convert("RGB")],
                             CHECK_PROMPT, sleep)
        if verdict.get("text") or verdict.get("extra_people") or verdict.get("colours_ok") is False:
            fixed = out / f"kf_{i:03d}_fixed.png"
            _image_call(client, ledger, cache, "fix",
                        [Image.open(dst).convert("RGB"), src, FIX_PROMPT.format(fix=verdict.get("fix") or "")],
                        aspect, fixed, sleep)
            fixed.replace(dst)
        keyframes.append(dst)
        prev = dst
        progress(i + 1, len(frames))
    return keyframes


def build_animatic(frames, keyframes, duration: float, size: tuple[int, int], out: Path) -> Path:
    starts = [0.0] + [(a["t"] + b["t"]) / 2 for a, b in zip(frames, frames[1:])]
    ends = starts[1:] + [duration]
    listing = out.with_suffix(".txt")
    with listing.open("w") as fh:
        for kf, s, e in zip(keyframes, starts, ends):
            fh.write(f"file '{kf.resolve()}'\nduration {e - s:.4f}\n")
        fh.write(f"file '{keyframes[-1].resolve()}'\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-vf", f"scale={size[0]}:{size[1]},fps={FPS},format=yuv420p", "-t", f"{duration:.3f}",
                    "-c:v", "libx264", "-crf", "16", str(out)], check=True)
    return out


def pieces(duration: float) -> tuple[list[float], float]:
    """Equal pieces of at most PIECE_MAX seconds that overlap by OVERLAP seconds."""
    if duration <= PIECE_MAX:
        return [0.0], duration
    n = math.ceil((duration - OVERLAP) / (PIECE_MAX - OVERLAP))
    length = (duration + (n - 1) * OVERLAP) / n
    return [k * (length - OVERLAP) for k in range(n)], length


def _download(uri: str, out: Path) -> None:
    import httpx
    headers = {"x-goog-api-key": settings.GEMINI_API_KEY} if settings.GEMINI_API_KEY else {}
    with httpx.stream("GET", uri, headers=headers, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with out.open("wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)


def _video_part(data: dict) -> dict | None:
    if isinstance(data.get("output_video"), dict):
        return data["output_video"]
    for step in data.get("steps") or []:
        for content in step.get("content") or []:
            if content.get("type") == "video":
                return content
    return None


def animate_piece(client, ledger, cache, animatic: Path, start: float, length: float, rule: str,
                  aspect: str, out: Path, sleep, poll=8.0) -> Path:
    model = settings.VIDEO_MOTION_MODEL
    cut = out.with_name(out.stem + "_in.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(animatic),
                    "-an", "-c:v", "libx264", "-crf", "16", str(cut)], check=True)
    prompt = MOTION_PROMPT.format(rule=rule)
    # Hash the keyframe pixels (not the re-encoded cut) so identical drawings hit the cache.
    key = cache.key(model, prompt, aspect, f"{start:.3f}", f"{length:.3f}", animatic)
    hit = cache.get(key, "mp4")
    if hit:
        ledger.add("motion", model, 0, 0, cached=True)
        shutil.copyfile(hit, out)
        return out
    estimate = length * MOTION_TOKENS_PER_SECOND * MOTION_PRICE_OUT * 1.15
    ledger.reserve("motion", estimate)
    f = _retry(lambda: client.files.upload(file=str(cut)), sleep)
    while f.state and str(f.state).endswith("PROCESSING"):
        sleep(2)
        f = client.files.get(name=f.name)
    r = _retry(lambda: client.interactions.create(
        model=model,
        input=[{"type": "text", "text": prompt}, {"type": "video", "uri": f.uri, "mime_type": "video/mp4"}],
        response_format={"type": "video", "resolution": "720p", "aspect_ratio": aspect, "delivery": "uri"},
        background=True), sleep)
    deadline = time.monotonic() + 1800
    while getattr(r, "status", None) not in ("completed", "failed", "cancelled", "incomplete"):
        if time.monotonic() > deadline:
            raise RuntimeError("motion: timed out after 30 minutes")
        sleep(poll)
        r = _retry(lambda: client.interactions.get(r.id), sleep)
    data = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    usage = data.get("usage") or {}
    tokens = (usage.get("total_output_tokens") or 0) + (usage.get("total_thought_tokens") or 0)
    ledger.add("motion", model, tokens, tokens * MOTION_PRICE_OUT if tokens else estimate)
    if data.get("status") != "completed":
        raise RuntimeError(f"motion: interaction {data.get('status')}")
    video = _video_part(data)
    if video and video.get("data"):
        out.write_bytes(base64.b64decode(video["data"]))
    elif video and video.get("uri"):
        _download(video["uri"], out)
    else:
        raise RuntimeError("motion: no video in the response")
    cache.put(key, "mp4", out)
    return out


def join(parts: list[Path], length: float, size: tuple[int, int], out: Path) -> Path:
    w, h = size
    args, chains = ["ffmpeg", "-v", "error", "-y"], []
    for i, p in enumerate(parts):
        args += ["-i", str(p)]
        chains.append(f"[{i}:v]scale={w}:{h},fps={FPS},tpad=stop_mode=clone:stop_duration=2,"
                      f"trim=0:{length:.3f},setpts=PTS-STARTPTS[v{i}]")
    last = "v0"
    for i in range(1, len(parts)):
        chains.append(f"[{last}][v{i}]xfade=transition=fade:duration={OVERLAP}:"
                      f"offset={i * (length - OVERLAP):.3f}[x{i}]")
        last = f"x{i}"
    chains.append(f"[{last}]format=yuv420p[out]")
    subprocess.run(args + ["-filter_complex", ";".join(chains), "-map", "[out]", "-c:v", "libx264",
                           "-crf", "18", "-movflags", "+faststart", str(out)], check=True)
    return out


def run(order: dict, progress=lambda p: None, client=None, sleep=time.sleep) -> Path:
    """Make manga.mp4 for a paid order. progress() gets 0-100."""
    work = settings.DATA_DIR / "jobs" / order["id"] / "video"
    work.mkdir(parents=True, exist_ok=True)
    cache = Cache(settings.DATA_DIR / "cache")
    ledger = Ledger(order["id"], settings.VIDEO_ORDER_BUDGET_USD)
    if client is None:
        from .pipeline import _client
        client = _client()

    video = Path(order["video"])
    duration, w, h = probe(video)
    duration = min(duration, settings.VIDEO_MAX_SECONDS)
    aspect, size = ("16:9", (1280, 720)) if w >= h else ("9:16", (720, 1280))
    frames = sample_frames(video, work / "src", duration, settings.VIDEO_KF_INTERVAL, size)
    middle = Image.open(frames[len(frames) // 2]["src"]).convert("RGB")
    info = _json_call(client, ledger, cache, "inspect", [middle], INSPECT_PROMPT, sleep)
    paint_out(frames, info.get("overlays") or [])
    rule = team_rule(info.get("teams"))
    progress(5)

    keyframes = draw_keyframes(client, ledger, cache, frames, settings.VIDEO_STYLE_REF, rule, aspect,
                               work / "kf", sleep, lambda i, n: progress(5 + 55 * i / n))
    animatic = build_animatic(frames, keyframes, duration, size, work / "animatic.mp4")
    starts, length = pieces(duration)
    parts = []
    for i, start in enumerate(starts):
        parts.append(animate_piece(client, ledger, cache, animatic, start, length, rule, aspect,
                                   work / f"motion_{i}.mp4", sleep))
        progress(60 + 35 * (i + 1) / len(starts))
    result = join(parts, length, size, work / "manga.mp4")
    progress(100)
    return result
