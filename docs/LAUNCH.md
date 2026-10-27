# Launch plan

## 1. Where to host

**Recommendation: one small cloud server in Singapore (or Tokyo), running `docker compose`.**

Why this shape:

- **The Gemini API is not available from mainland China or Hong Kong.** The server that calls it must sit in a supported region. Singapore and Tokyo are the closest to Chinese users.
- **A server outside mainland China needs no ICP filing (备案).** A mainland server does, and the filing takes weeks.
- **Each manga takes several minutes** (12–17 image generations). Serverless platforms with short request timeouts (Vercel, Netlify functions) don't fit; a long-running process with a worker thread does. No GPU is needed because Gemini does the drawing.
- **Payment notifications** from WeChat Pay, Alipay and Stripe need a fixed public HTTPS address. Caddy provides the certificate.

Good options, any of which runs this repo unchanged:

| Provider | Product | Size to start | Notes |
| --- | --- | --- | --- |
| Alibaba Cloud (阿里云) | ECS, Singapore region | 2 vCPU / 4 GB | Chinese console and billing; add OSS later for files |
| Tencent Cloud (腾讯云) | Lighthouse, Singapore | 2 vCPU / 4 GB | Cheapest to start |
| Google Cloud | Compute Engine, asia-southeast1 | e2-medium | Same network as Gemini |

Files: start on the server's disk (`./data`). Delete uploaded videos after 7 days. When volume grows, move uploads and PDFs to object storage (Alibaba OSS or Google Cloud Storage) and serve downloads from signed URLs.

Scaling later: run more worker processes, or move `app/worker.py` to a queue (Redis + RQ, or Cloud Run Jobs). Postgres replaces SQLite once you run more than one server.

## 2. Pay-and-buy flow

What the buyer does: turn the dials → drop a clip → scan the QR → wait on the same page → tap "Download the PDF". No account, no form.

What the server does:

1. On upload it stores the video and creates an order with an unguessable id.
2. It asks the payment provider for a QR payload and returns it; the page renders the QR.
3. The provider calls `/api/pay/notify`. The server checks the signature **and the amount** before marking the order paid. The page never decides that an order is paid.
4. The worker draws the manga and the QR area turns into a download link.
5. If drawing fails, the order shows "failed" with the order number; refund it from the merchant dashboard.

### Which payment provider

| | WeChat Pay Native (扫码支付) | Alipay 当面付 | Stripe Checkout |
| --- | --- | --- | --- |
| Who can open it | Mainland business licence (个体工商户 is enough) | Same | Company or sole trader in a Stripe country (HK, SG, US, JP…) |
| Buyer pays with | WeChat | Alipay | Card, Alipay, WeChat Pay |
| Fee | about 0.6% | about 0.6% | higher (about 3–4% for cards and wallets) |
| One QR for both wallets | No | No | Yes (the QR opens a checkout page) |

**If your buyers are in mainland China:** register a 个体工商户, open WeChat Pay first (most buyers will use it), then add Alipay. Set `PAYMENT_PROVIDER=wechat`. If you want one QR that both apps can scan, use a licensed aggregator (聚合支付) instead.

**If you have, or will open, a company outside mainland China:** Stripe is the fastest path and covers international buyers too. Set `PAYMENT_PROVIDER=stripe`.

Avoid personal receive codes (个人收款码) for this: the server can't confirm the payment automatically, and the platforms restrict commercial use.

### Price

Each manga makes roughly 12–17 calls to the Gemini image model plus one text call. At current list prices that is roughly ¥15–30 of API cost per manga, so ¥39 (the placeholder in `.env.example`) is close to the floor. Check Google's current pricing before you set it.

## 3. Things to settle before going public

- **Style names.** "Naruto", "Slam Dunk", "Demon Slayer", "One Piece" and "Miyazaki" are trademarks or a real person's name. Using them to sell a product invites takedowns. Consider labels like "Shonen Ninja", "90s Hoops", "Taisho Blade", "Pirate Adventure", "Meadow Film" on the public site.
- **People in uploaded videos.** Add terms of service and a privacy notice: buyers must have the right to use the footage (many clips will show minors), and videos are deleted after 7 days.
- **Abuse limits.** Rate-limit uploads per IP and keep `MAX_UPLOAD_MB` modest.
- **Gemini key.** Create a fresh key for production, keep it only in the server's `.env`, and set a budget alert in Google Cloud.
