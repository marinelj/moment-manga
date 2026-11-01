# Railway sandbox deployment

The FastAPI application must serve both the site and `/api/` on the same origin. GitHub Pages remains a static preview and cannot run the checkout backend.

## Service settings

- Deploy the repository's `main` branch from the repository root. Railway detects the root `Dockerfile`.
- Enable public HTTP networking and generate a Railway domain.
- Set the health check path to `/healthz`.
- Attach a persistent volume at `/data`; the SQLite database, uploads, and generated files use this directory.
- Keep one application replica because the worker and SQLite database are designed for one instance.

## Variables

Set these in the Railway service's private Variables panel. Do not add the values to GitHub or the repository.

```text
PAYMENT_PROVIDER=alipay_web
ALIPAY_ENV=sandbox
ALIPAY_BOOK_CENTS=110
ALIPAY_SANDBOX_JSON=<private sandbox config JSON>
GEMINI_API_KEY=<replacement Gemini key>
PUBLIC_URL=<generated Railway HTTPS origin>
DATA_DIR=/data
MAX_UPLOAD_MB=300
```

Leave `ALIPAY_NOTIFY_URL` empty for this sandbox deployment. The order status route reconciles payment with Alipay trade query. Production requires a public HTTPS notification endpoint and separate production credentials.

After setting the service variables, deploy the commit and open the Railway domain. The home, book preview, checkout form, API, and payment return route will share that origin. Checkout becomes available when `/api/config` reports `checkout_ready: true`.
