# Basketball page API contract

The browser uses these endpoints when served by the FastAPI application. GitHub Pages is a static preview and cannot reach a same-origin API. The book-and-redrawn-video product remains disabled until its video generation and download are implemented.

## Checkout readiness

`GET /api/config` returns `checkout_ready`, `products`, `charges`, and `max_upload_mb`. The server returns `checkout_ready: false`, no products, and no charges unless Alipay web payment is configured and the manga worker has a Gemini key, `ffmpeg`, and `ffprobe`. An available book charge has this shape:

```json
{"products":["book"],"charges":{"book":{"amount":"39.00","currency":"CNY"}}}
```

The amount above is an API example, not a price choice. The server reads the approved amount from `ALIPAY_BOOK_CENTS`; the page displays it alongside the USD catalog price before checkout. The browser must never submit an amount.

## Order creation

`POST /api/orders` accepts multipart form fields:

| Field | Required | Value |
| --- | --- | --- |
| `video` | Yes | MP4, MOV, or WebM clip, at most `MAX_UPLOAD_MB`, with a video stream and duration from 0.5 to 120 seconds |
| `photo` | No | Valid JPEG, PNG, or WebP image, at most 20 MB and 40 million pixels |
| `product` | Yes | `book` |
| `lang` | Yes | `en` or `zh` |

The response is `{"id":"...","status":"unpaid","payment":{"pay_url":"/api/orders/.../pay","charge":{"amount":"...","currency":"CNY"}}}`. It sets an HttpOnly buyer cookie scoped to `/api`. Opening `pay_url` with that cookie renders the SDK-signed Alipay POST form. The payment page is never an authority for order status.

## Status and fulfillment

`GET /api/orders/{id}` requires the buyer cookie for Alipay web orders. It returns `id`, `status`, and `progress`. An unpaid order also returns its payment object. Status values are `unpaid`, `paid`, `processing`, `done`, and `failed`. A done book order returns `downloads.book` pointing to `/api/orders/{id}/manga.pdf`; that download requires the same buyer cookie.

The asynchronous `POST /api/pay/notify` verifies the RSA2 signature, application, seller, order number, amount, and trade state before a paid transition. Repeated notifications are idempotent. The status endpoint periodically queries Alipay for an unpaid order to reconcile a missing notification. The browser return route only reports status.

Administrative query, refund, refund query, and close endpoints are under `/api/admin/alipay` and require `Authorization: Bearer <ALIPAY_ADMIN_TOKEN>`.

## Remaining product work

The `all` package has no checkout route or redrawn video download. It needs an actual frame-by-frame video pipeline, fulfillment tests, an approved CNY price, and a sample before being enabled. Production sales also require merchant approval, public HTTPS notification testing, and a real small-payment/refund check. See [Alipay setup](ALIPAY_WEB.md).
