# Moment Manga

Upload a short video, pick a theme and an art style on two telephone dials, pay by QR code, and get back a 3–5 page Japanese-style manga as a PDF.

The whole site is one page with four controls, in English at `/` and Chinese at `/zh/`: two rotary dials, a video upload box and a payment QR area.

- Left dial (Sports, Travel & Home Life, Portrait Photos, Comedy) swaps the line-art background.
- Right dial (Hayao Miyazaki, Naruto, Slam Dunk, Demon Slayer, One Piece) restyles the whole page and sets the art style of the manga.

## How it works

```
browser ──upload──▶ /api/orders ──▶ payment provider ──QR──▶ browser
                                     │ (server-side notify, signature checked)
                                     ▼
                         worker: ffmpeg frames → Gemini storyboard → Gemini panels → lettering → PDF
                                     ▼
                     QR area turns into "Download the PDF"
```

| Path | What it is |
| --- | --- |
| `web/index.html` | The single page (no build step) |
| `app/main.py` | FastAPI server: upload, order status, payment notify |
| `app/payments.py` | WeChat Pay Native, Alipay 当面付, Stripe Checkout, and a demo provider |
| `app/pipeline.py` | Video → manga pipeline (Gemini text + image models) |
| `app/worker.py` | Background thread that processes paid orders |
| `docs/LAUNCH.md` | Where to host it and how to set up payments |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # also needs ffmpeg on PATH
export GEMINI_API_KEY=...                 # never commit this
uvicorn app.main:app --reload
```

Open http://localhost:8000. `PAYMENT_PROVIDER` defaults to `demo`: the QR code points to `/api/dev/pay/<order>`, so scanning it with a phone on the same network (set `PUBLIC_URL` to your LAN address) marks the order paid.

## Deploy

```bash
cp .env.example .env    # fill in keys on the server only
docker compose up -d --build
```

Caddy in `docker-compose.yml` gets an HTTPS certificate for `DOMAIN` automatically. See `docs/LAUNCH.md`.

## Secrets

All keys come from environment variables (`.env` on the server, which is git-ignored). Merchant certificates go in `./secrets/`, also git-ignored.
