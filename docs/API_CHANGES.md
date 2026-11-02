# Basketball page API contract

The browser uses these endpoints when served by the FastAPI application. GitHub Pages is a static preview and cannot reach a same-origin API. Two products exist: `book` (PDF) and `all` (PDF plus redrawn video). `all` is offered only when `ALIPAY_ALL_CENTS` is set.

## Checkout readiness

`GET /api/config` returns `checkout_ready`, `products`, `charges`, `max_upload_mb`, `video_max_seconds`, and `generation` (`live`, or `sample` when paid orders receive sample files instead of generated manga; the page may show a "test mode" note when it is `sample`). The server returns `checkout_ready: false`, no products, and no charges unless Alipay web payment is configured and the manga worker has a Gemini key, `ffmpeg`, and `ffprobe`. An available book charge has this shape:

```json
{"products":["book","all"],"charges":{"book":{"amount":"39.00","currency":"CNY"},"all":{"amount":"79.00","currency":"CNY"}}}
```

The amount above is an API example, not a price choice. The server reads the approved amounts from `ALIPAY_BOOK_CENTS` and `ALIPAY_ALL_CENTS`; the page displays it alongside the USD catalog price before checkout. The browser must never submit an amount.

## Order creation

`POST /api/orders` accepts multipart form fields:

| Field | Required | Value |
| --- | --- | --- |
| `video` | Yes | MP4, MOV, or WebM clip, at most `MAX_UPLOAD_MB`, with a video stream and duration from 0.5 to 120 seconds |
| `photo` | No | Valid JPEG, PNG, or WebP image, at most 20 MB and 40 million pixels |
| `product` | Yes | `book` or `all` (when listed in `products`) |
| `lang` | Yes | `en` or `zh` |

For `all`, the clip must be at most `video_max_seconds` (default 15) or the server answers 400 and creates no order.

The response is `{"id":"...","status":"unpaid","payment":{"pay_url":"/api/orders/.../pay","charge":{"amount":"...","currency":"CNY"}}}`. It sets an HttpOnly buyer cookie scoped to `/api`. Opening `pay_url` with that cookie renders the SDK-signed Alipay POST form. The payment page is never an authority for order status.

## Status and fulfillment

`GET /api/orders/{id}` requires the buyer cookie for Alipay web orders. It returns `id`, `status`, and `progress`. An unpaid order also returns its payment object. Status values are `unpaid`, `paid`, `processing`, `done`, and `failed`. A done order returns `downloads.book` pointing to `/api/orders/{id}/manga.pdf`. A done `all` order also returns `downloads.clip` pointing to `/api/orders/{id}/manga.mp4`. Both downloads require the same buyer cookie. For `all`, `progress` runs 0 to 30 while the book is made and 30 to 100 while the video is made; expect about 10 to 20 minutes in total.

The asynchronous `POST /api/pay/notify` verifies the RSA2 signature, application, seller, order number, amount, and trade state before a paid transition. Repeated notifications are idempotent. The status endpoint periodically queries Alipay for an unpaid order to reconcile a missing notification. The browser return route only reports status.

Administrative query, refund, refund query, and close endpoints are under `/api/admin/alipay` and require `Authorization: Bearer <ALIPAY_ADMIN_TOKEN>`. `GET /api/admin/orders/{id}/costs` (same token) returns the order's model-call cost ledger: `total_usd` and one line per call with `step`, `model`, `units`, `usd`, and `cached`.

## Video pipeline

`app/video.py` makes the `all` video: keyframes every 0.5 s with the image model (each drawn with the previous one for consistency), a cheap check per keyframe with one fix when flagged, then the motion model in overlapping pieces of at most 6.6 s, joined with 1 s crossfades. Broadcast graphics are painted out first and team jersey colours are kept. Every paid call is cached by a hash of its inputs, so a retried order does not pay twice, and is written to the cost ledger. An order stops before any call that would pass `VIDEO_ORDER_BUDGET_USD` (default 4.00). If the video fails, the order is `failed` but the finished book stays on disk for a manual refund or retry.

## Remaining product work

Selling `all` needs an approved CNY price in `ALIPAY_ALL_CENTS`, a higher Gemini spend-rate limit (the default limit throttles at about $10 per hour, which is 3 to 4 videos), and a reviewed sample. Production sales also require merchant approval, public HTTPS notification testing, and a real small-payment/refund check. See [Alipay setup](ALIPAY_WEB.md).
