"""Sample fulfilment for GENERATION_MODE=sample: no model calls, no model costs.

A paid order gets a one-page PDF marked SAMPLE (with a frame from the buyer's own clip when
ffmpeg can read it) and, for the "all" package, the demo redrawn clip. This lets the whole
flow (upload, payment, status, download) be tested against the Alipay sandbox for free.
"""
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from . import pipeline, settings

DEMO_CLIP = Path(__file__).resolve().parent.parent / "web" / "demo" / "rerendered.mp4"


def _frame(video: Path, out: Path) -> Image.Image | None:
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=1400:-2", str(out)], check=True, timeout=60)
        return Image.open(out).convert("RGB")
    except (OSError, subprocess.SubprocessError):
        return None


def run(order: dict, with_video: bool) -> tuple[Path, Path | None]:
    work = settings.DATA_DIR / "jobs" / order["id"]
    work.mkdir(parents=True, exist_ok=True)
    page = Image.new("RGB", (pipeline.PAGE_W, pipeline.PAGE_H), "white")
    draw = ImageDraw.Draw(page)
    big, small = pipeline._font(110), pipeline._font(44)
    draw.text((pipeline.PAGE_W // 2, 260), "SAMPLE", font=big, fill="black", anchor="mm")
    lines = ["Sandbox test order " + order["id"][:8],
             "Payment worked. No manga was generated,",
             "so no model costs were spent.",
             "Switch to real generation on /admin."]
    for i, line in enumerate(lines):
        draw.text((pipeline.PAGE_W // 2, 420 + i * 70), line, font=small, fill="black", anchor="mm")
    if order.get("video"):
        frame = _frame(Path(order["video"]), work / "sample_frame.png")
        if frame:
            frame.thumbnail((pipeline.PAGE_W - 2 * pipeline.MARGIN, pipeline.PAGE_H - 900))
            page.paste(frame, ((pipeline.PAGE_W - frame.width) // 2, 760))
    pdf = work / "manga.pdf"
    page.save(pdf, resolution=200)
    mp4 = None
    if with_video:
        mp4 = work / "manga.mp4"
        shutil.copyfile(DEMO_CLIP, mp4)
    return pdf, mp4
