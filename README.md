# Moment Manga

Moment Manga turns a short basketball clip into a 3–5 page black-and-white manga PDF. The English page is at `/`; the Chinese page is at `/zh/`. Buyers can upload a clip and an optional lead-character photo, pay through Alipay AI web application payment, and download the PDF when generation finishes.

The **manga book** is the only product currently available for checkout. The book-and-redrawn-video package is shown as coming soon because video redrawing and delivery are not implemented. GitHub Pages serves a static preview; it cannot process uploads or payments.

## Showcase assets to add

The static preview includes SVG page placeholders and video placeholders. Replace them by adding these files under `web/demo/`:

| File | Suggested size |
| --- | --- |
| `manga-page-01.jpg` | 1200 × 1800 px, portrait |
| `manga-page-02.jpg` | 1200 × 1800 px, portrait |
| `original.mp4` | 1920 × 1080 px, 10–15 seconds |
| `rerendered.mp4` | 1920 × 1080 px, same duration and framing as `original.mp4` |

The two videos should use browser-friendly H.264 encoding and stay small enough to load quickly on mobile.

## Run locally

Use Python 3.12 and install `ffmpeg` (including `ffprobe`) on `PATH`:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

`PAYMENT_PROVIDER=demo` is the default and displays the page without live checkout. To test the Alipay sandbox, follow [the payment setup guide](docs/ALIPAY_WEB.md). Set `GEMINI_API_KEY` before enabling checkout: the server only accepts paid orders when the generation tools are configured. The CNY payment amount must be set explicitly in `ALIPAY_BOOK_CENTS`; the displayed USD catalog price does not set it.

Run the local tests with:

```bash
.venv/bin/python -m pytest -q
```

## How it works

1. The browser sends the clip, optional photo, product, and language to `POST /api/orders`.
2. The server validates the files and records the server-configured CNY amount. It returns a same-origin Alipay payment link and an HttpOnly buyer cookie.
3. Alipay's signed notification or a server-side trade query confirms payment. A browser redirect never marks an order paid.
4. The worker extracts video frames, generates manga panels with Gemini, and assembles a PDF. The buyer's page polls for progress and a protected download link.
5. Uploaded inputs are deleted seven days after order creation once the order is unpaid, done, or failed. Generated PDFs require an operator retention policy.

| Path | Purpose |
| --- | --- |
| `web/index.html` | English and Chinese single-page UI |
| `app/main.py` | Upload, checkout, status, and download API |
| `app/alipay_web.py` | Signed Alipay web checkout and payment verification |
| `app/pipeline.py` | Video-to-manga PDF generation |
| `app/worker.py` | Paid-order processing and input cleanup |
| `docs/API_CHANGES.md` | Current browser/API contract |
| `docs/ALIPAY_WEB.md` | Alipay setup, operations, and launch checks |

For deployment, copy `.env.example` to a server-only `.env`, fill in approved values, and run `docker compose up -d --build`. See [the launch guide](docs/LAUNCH.md). Never commit `.env`, merchant keys, or uploaded files.
