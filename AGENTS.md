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

- FastAPI (`app/main.py`) serves the page and `/api/` on one origin. Railway deployment is prepared (see `docs/RAILWAY.md`): one replica, SQLite and files on the `/data` volume. GitHub Pages hosts the customer page and uses the public gateway API for checkout.
- Local public hosting also runs behind the shared gateway at `https://marine-api.tunnel.tungo.cn/manga/`: the gateway owns port 8080 and forwards to this app on loopback 18081. Gateway deployment and route registration live in the separate `public-api-gateway` repository; its LaunchAgent sets `PUBLIC_URL` to the public `/manga` base and keeps this local instance in sandbox/sample mode without a Gemini key.
- `app/worker.py` takes paid orders one at a time: book first (`app/pipeline.py`), then the video for `all` (`app/video.py`). If the video fails, the order is `failed` but the PDF is kept.
- Gemini API style (`/admin`, else `GEMINI_API_STYLE`, default `standard`), recorded on each order like its mode. `batch` sends all book panels of a real order as one Gemini Batch API job at half price; Google aims for 24 hours and many jobs finish much sooner. A batch order waits in its own worker thread so it doesn't hold up the queue. The job name, the storyboard and every finished panel are saved in the order's folder, so a restarted order waits on the same job instead of paying twice. Panels the job doesn't return, and every panel still missing after `BATCH_MAX_WAIT_HOURS` (default 24, then the job is cancelled), are drawn with standard calls. The storyboard and the whole video stay on standard calls: video keyframes are drawn in a chain and Omni has no batch price.
- Download period: finished files are deleted `DOWNLOAD_DAYS` (default 7) after the order is done, and the buyer's email address goes with them. Orders finished before 2026-11-09 have no `done_at` and are kept.
- Ready email (optional): a buyer may leave an address at checkout. When the order is done, the Mac emails the order link (`ORDER_PAGE_URL`, plus `zh/` for Chinese orders, `?order=<id>&t=<buyer token>`) over SMTP. Off until `SMTP_HOST` and `SMTP_FROM` (or `SMTP_USER`) are set. The token is kept in plain text only until the email is sent. A failed send is retried hourly for a day and never fails the order. Payment-test orders get the email too, marked [Test].
- Video pipeline: keyframes every 0.5 s, each drawn with the previous one for consistency; a cheap check of every keyframe, with one fix that is kept only if it still looks like manga; a held-keyframe animatic; motion in pieces of up to 6.6 s; 1 s crossfades between pieces.
- `GENERATION_MODE` decides what a paid order gets: `live` runs the real pipelines; `sample` delivers a one-page SAMPLE PDF (with a frame from the buyer's clip) and the demo clip, with no model calls. It defaults to `sample` while `ALIPAY_ENV=sandbox`. Each order records the mode it was created under, so a test order never reaches the paid pipelines, even after a restart in `live`. Checkout stays closed if it is `sample` in production, unless `ALLOW_PRODUCTION_PAYMENT_TEST=1` is set for a deliberate real-money payment test (refund it from the admin routes afterwards).
- `/admin` is an open debug page with no token (owner's decision, 2026-11-03; don't add one back). It switches new orders between mocked (`sample`) and real (`live`) Gemini at runtime (stored in the `app_settings` table, overriding `GENERATION_MODE`), and lists recent orders with their mode and model cost. The same file is published to GitHub Pages at `/moment-manga/admin/` (by `scripts/build_site.sh`); there it asks for the server address.
- Cross-origin: a page on another site (GitHub Pages) can call the API when the server lists it in `CORS_ORIGINS`. Buyers then authenticate with the `buyer_token` from the order response, sent as the `X-Buyer-Token` header, or as `?t=<token>` on the payment and download links. Cookies alone fail cross-site in Safari.
- Every paid model call is cached by an input hash (a retried order doesn't pay twice) and logged in the `order_costs` table. `VIDEO_ORDER_BUDGET_USD` (default 4.00) stops an order before it overspends. Admins can see the costs at `GET /api/admin/orders/{id}/costs`.

## Models and their roles

| Variable | Default model | Role | Cost per order |
| --- | --- | --- | --- |
| `GEMINI_TEXT_MODEL` | `gemini-3.8-flash` | Book storyboard: pages, panels, captions, sound effects | < $0.01 |
| `GEMINI_IMAGE_MODEL` | `gemini-3-pro-image` | Draws the book panels | $0.134 a panel, $0.067 on Batch: about $1.20–2.70 a book, half on Batch |
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
- Done: book pipeline, Alipay web checkout (sandbox on Railway), video pipeline and `all` product in the backend (PR #1), storyboard model fix (PR #3), free sandbox payment testing with `GENERATION_MODE=sample` (2026-11-02), `/admin` mocked/real switch and real-money payment test flag (2026-11-03), `/admin` Standard/Batch API style switch, book panel costs in the cost ledger, 7-day download period and optional ready email (2026-11-09).
- Hosting: the backend runs on the owner's Mac behind the shared public `/manga` gateway. GitHub Pages hosts the customer and admin pages. Railway is prepared (`docs/RAILWAY.md`) but not set up.
- Front end: English and Chinese checkout pages label sample mode, payment-test processing, sample downloads, and sample video previews. Payment-test orders do not show the live-generation wait estimate.
- Verified 2026-11-03: local `book` sandbox checkout in Safari at the configured ¥1.10 charge; server trade query returned `TRADE_SUCCESS`, sample PDF downloaded with the buyer cookie and matched the delivered file, full SDK refund returned `fund_change=Y`, and refund query returned `REFUND_SUCCESS`. Zero model calls and $0 model cost. Public HTTPS notification delivery and production remain unverified.
- Verified 2026-11-03: shared public gateway routing at `/manga/`, including upload, sample-mode status/configuration, scoped secure buyer cookies, prefixed payment URLs, Chinese-page redirect, and the signed public payment return URL. No additional payment or paid generation was submitted; the local instance still omits `ALIPAY_NOTIFY_URL`, so public payment-notification delivery remains unverified.
- Before selling `all`: set `ALIPAY_ALL_CENTS`; raise the Gemini spend-rate limit (the default throttles at about $10 an hour, which is 3–4 videos); review one live sample.
- Last measured: a 6 s clip through the video pipeline took about 5 minutes and cost $1.62.

- Front end 2026-11-04: static builds configure `MANGA_API_BASE` (default `https://marine-api.tunnel.tungo.cn/manga`); backend-served pages stay on their own API mount. Buyer tokens persist per API/order in localStorage, authenticate status via `X-Buyer-Token`, and authorize payment/download/playback links with `?t=`. Completed orders can be restored after reload.
- Verified 2026-11-04 after PRs #6–#9: the Mac instance on loopback 18081 returns public admin status as 200 JSON without a token, permits the GitHub Pages buyer-token CORS preflight, and the Pages admin displays MOCKED in Safari. Sandbox/sample mode is retained with no Gemini key.

## Requests between agents

Add a dated line here; the other agent removes it when done.

- 2026-11-09 Claude → Codex (owner approved: order link plus optional email, 7-day download period shown on the pages). Fields are in `docs/API_CHANGES.md`.
  1. Batch wait: when `/api/config` has `generation: "live"` and `api_style: "batch"`, say before payment that the book can take up to `batch_max_wait_hours` hours (often much sooner) and that we'll email or they can come back with their order link. While an order whose status has `api_style: "batch"` is processing, show that instead of the 5-minute wait.
  2. Order link: after an order is created, show `<page>?order=<id>&t=<buyer_token>` (the Chinese page for Chinese orders) with a copy button and a QR code, and ask the buyer to save it. On load, `?order=` with `t=` restores that order: store the token, poll its status, show the downloads. Keep the active order in localStorage too, not only sessionStorage, so it survives closing the tab.
  3. Email: when `/api/config` has `ready_email: true`, show an optional email field at checkout and send it as `email` in the `POST /api/orders` form. The server answers 400 for an invalid address. Hide the field when `ready_email` is false.
  4. Download period: before payment and next to the downloads, say the files are deleted `download_days` days after they are ready. Next to the downloads show the date from `files_expire_at` (Unix seconds). When the status has `files_expired: true`, say the files were deleted after that period and show no download links.
