# AGENTS.md: shared notes for every coding agent on Moment Manga

Two agents work on this repository: **Claude** (backend, in Claude Code) and **Codex** (front end). Both read this file before starting and update it in the same pull request as any change that affects it. The owner is marine lee; nothing merges to `main` without their review.

## Who owns what

| Area | Owner | Files |
| --- | --- | --- |
| Backend: API, payments, order worker, book and video pipelines | Claude | `app/`, `tests/`, `requirements.txt`, `Dockerfile`, `.env.example` |
| Front end: the page in English and Chinese | Codex | `web/`, `scripts/build_site.sh`, `.github/workflows/pages.yml` |
| Shared contract | Both | `docs/API_CHANGES.md` (the API is defined there), this file |

If you need a change on the other side, write it under **Requests between agents** below rather than editing the other side's files.

## What the product is

A buyer uploads one basketball clip (about 10–15 s) and an optional hero photo, pays with Alipay, and gets:

- `book`: a 3–5 page black-and-white manga PDF ($4.99 catalog price)
- `all` ("Everything"): the book plus the clip redrawn as a moving manga MP4 ($9.99 catalog price)

The CNY amount charged comes only from server variables (`ALIPAY_BOOK_CENTS`, `ALIPAY_ALL_CENTS`, in fen). The prices on the page are display text. `all` is off sale while `ALIPAY_ALL_CENTS` is empty.

## Architecture

- FastAPI (`app/main.py`) serves the page and `/api/` on one origin. Deployment is Railway (see `docs/RAILWAY.md`): one replica, SQLite and files on the `/data` volume. GitHub Pages is a static preview with no checkout.
- `app/worker.py` takes paid orders one at a time: book first (`app/pipeline.py`), then the video for `all` (`app/video.py`). If the video fails, the order is `failed` but the PDF is kept.
- Video pipeline: keyframes every 0.5 s, each drawn with the previous one for consistency; a cheap check of every keyframe, with one fix that is kept only if it still looks like manga; a held-keyframe animatic; motion in pieces of up to 6.6 s; 1 s crossfades between pieces.
- `GENERATION_MODE` decides what a paid order gets: `live` runs the real pipelines; `sample` delivers a one-page SAMPLE PDF (with a frame from the buyer's clip) and the demo clip, with no model calls. It defaults to `sample` while `ALIPAY_ENV=sandbox`. Each order records the mode it was created under, so a test order never reaches the paid pipelines, even after a restart in `live`. Checkout stays closed if it is `sample` in production.
- Every paid model call is cached by an input hash (a retried order doesn't pay twice) and logged in the `order_costs` table. `VIDEO_ORDER_BUDGET_USD` (default 4.00) stops an order before it overspends. Admins can see the costs at `GET /api/admin/orders/{id}/costs`.

## Models and their roles

| Variable | Default model | Role | Cost per order |
| --- | --- | --- | --- |
| `GEMINI_TEXT_MODEL` | `gemini-3.8-flash` | Book storyboard: pages, panels, captions, sound effects | < $0.01 |
| `GEMINI_IMAGE_MODEL` | `gemini-3-pro-image` | Draws the book panels | about $1–2 |
| `VIDEO_CHECK_MODEL` | `gemini-3.5-flash-lite` | Finds score bars and team colours; checks each keyframe | about $0.01 |
| `VIDEO_IMAGE_MODEL` | `gemini-3.1-flash-image` | Video keyframes and their fixes | about $0.07 each, $1.60–2.20 a clip |
| `VIDEO_MOTION_MODEL` | `gemini-omni-1.1-flash` | Animates the keyframes into motion | about $0.66 per 6.5 s piece |

Google retires models: `gemini-2.5-flash` was refused for new users on 2026-11-02. If a call fails with "no longer available", switch that variable to the model the error message names, then test it with one cheap call.

Later phases, not in the code yet: Qwen-Image-Edit with a style LoRA on RunPod replaces the keyframe model (Phase 2); an open video model (LTX-Video or HunyuanVideo) replaces Omni (Phase 3).

## Rules

- Keys and merchant credentials live only in the server's environment (Railway Variables). Never commit `.env`, keys, or `.alipay-sandbox.json`.
- Never train a model on Gemini or Omni output (Gemini API terms). Style training data must come from our own or open models.
- The browser never sends an amount. The server decides prices.
- Don't use real manga titles or artist names anywhere on the site.
- Run `python -m pytest -q` before pushing backend changes. Tests use a fake Gemini client; real model calls cost money, so make them only when needed and note the cost in the PR.
- Work on a branch and open a pull request against `main`.

## Status (update when it changes)

- Front end: `all` checkout uses the configured clip-duration limit; long clips can still buy the book. English and Chinese processing states show the expected wait, and completed clips play above the PDF/MP4 download links.
- Done: book pipeline, Alipay web checkout (sandbox on Railway), video pipeline and `all` product in the backend (PR #1), storyboard model fix (PR #3), free sandbox payment testing with `GENERATION_MODE=sample` (2026-11-02).
- Front end: English and Chinese checkout pages label sample mode, payment-test processing, sample downloads, and sample video previews. Payment-test orders do not show the live-generation wait estimate.
- Before selling `all`: set `ALIPAY_ALL_CENTS`; raise the Gemini spend-rate limit (the default throttles at about $10 an hour, which is 3–4 videos); review one live sample.
- Last measured: a 6 s clip through the video pipeline took about 5 minutes and cost $1.62.

## Requests between agents

Add a dated line here; the other agent removes it when done.
