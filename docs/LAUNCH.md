# Launch guide

## Host the application

Run the FastAPI server and its worker on a host that can reach the configured Gemini API and the Alipay gateway. The included `Dockerfile` installs Python 3.12, `ffmpeg`, and CJK fonts. `docker-compose.yml` runs one application container and a Caddy HTTPS proxy; the worker and SQLite store are designed for one application instance.

```bash
cp .env.example .env
# Fill .env with server-only secrets, an approved CNY amount, and your HTTPS domain.
docker compose up -d --build
```

The `DOMAIN` must resolve to the server. Set `PUBLIC_URL` to the exact public HTTPS origin. Configure `ALIPAY_NOTIFY_URL` to `https://<your-domain>/api/pay/notify`. Mount persistent `./data` and back it up. Keep `.env`, `.alipay-sandbox.json`, merchant keys, and uploaded data outside version control.

The static GitHub Pages page is a preview. Live checkout requires serving the page from this FastAPI origin or routing its `/api/` requests to this server on the same origin.

## Payment and fulfillment checks

Follow [the Alipay web payment guide](ALIPAY_WEB.md) for the sandbox and production credentials. The server disables checkout when the Gemini key or video tools are missing, but that check cannot establish that Gemini generation or the merchant account will succeed. Before accepting production orders:

1. Approve and set the book's actual CNY charge. Reconcile its displayed USD catalog price with the payment amount shown on the page.
2. Complete a sandbox upload, page-pay, notification, trade-query, PDF generation, and buyer-cookie download test.
3. Test the production HTTPS callback with a small real transaction and refund using the approved application.
4. Publish terms, privacy information, a support contact, and a retention policy for generated PDFs. Inputs are deleted after seven days for eligible Alipay orders; generated PDFs remain on disk until removed by an operator.
5. Monitor failed jobs and arrange refunds or support when fulfillment fails.

The redrawn-video package is disabled. Its checkout must stay disabled until generation, delivery, price approval, and tests are complete.
