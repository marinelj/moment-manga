# Alipay AI web application payment

The FastAPI checkout uses `alipay.trade.page.pay` through the official Python SDK. It supports the **book** product. The **all** package remains unavailable for checkout until its redrawn video can be generated and delivered. This prevents a buyer from paying for an unimplemented deliverable.

## Local sandbox

The application reads the verified `.alipay-sandbox.json` at the project root. Keep that file outside version control with owner-only permissions. Set `PAYMENT_PROVIDER=alipay_web`, `ALIPAY_ENV=sandbox`, and an explicitly approved `ALIPAY_BOOK_CENTS` value in the local environment. The USD catalog display does not determine the CNY charge; the CNY amount is set on the server and shown before the buyer opens the payment page.

Run `uvicorn app.main:app` from the project root with Python 3.12, the dependencies in `requirements.txt`, `ffmpeg` and `ffprobe` on `PATH`, and a configured `GEMINI_API_KEY`. Without those generation prerequisites, `/api/config` reports checkout unavailable and `POST /api/orders` refuses payment orders. `PUBLIC_URL` must point to the address where the buyer can open this FastAPI server. The payment link opens a same-origin page that renders the signed Alipay POST form. The default return page is `PUBLIC_URL/api/pay/return`; it displays a neutral status until the server confirms payment. On local development without a public HTTPS callback, omit `ALIPAY_NOTIFY_URL`; the order status endpoint queries Alipay to reconcile a missing notification.

The server receives notifications at `POST /api/pay/notify`. It verifies the SDK signature, application, seller, order number, exact CNY amount, and transaction status before changing the persisted order state. Repeated notifications are recorded once. The buyer's order and PDF routes require an HttpOnly order cookie.

## Production setup

Configure one approved production application with `ALIPAY_ENV=production`, `ALIPAY_APP_ID`, `ALIPAY_SELLER_ID`, raw PKCS#1 `ALIPAY_APP_PRIVATE_KEY`, and `ALIPAY_PUBLIC_KEY`. Keep all values in server secret storage; never place them in `web/` or commit them. Set a public HTTPS `PUBLIC_URL` and `ALIPAY_NOTIFY_URL` pointing to the deployed `/api/pay/notify`. Production startup rejects an HTTP public or notification URL. Configure a strong `ALIPAY_ADMIN_TOKEN` for the protected query, refund, refund query, and close routes. Do not share it with the browser.

Before production sales, approve the CNY amount and any USD to CNY pricing policy, complete a real HTTPS notification test, and verify a small transaction and refund using the approved application. Sandbox configuration and a local payment form alone do not establish production readiness.

## Data retention

Uploaded clips and reference photos for Alipay web orders are deleted after seven days once an order is unpaid, done, or failed; active paid and processing orders retain their inputs until fulfillment finishes. The worker checks hourly. Buyer order cookies last 30 days. Generated PDFs are retained in the server's `DATA_DIR/jobs` until the operator deletes them; back up and remove them according to the site's published retention policy before launch.
