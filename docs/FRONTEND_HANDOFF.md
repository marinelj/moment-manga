# Front-end hand-off: wire the "Everything" package into the page

The backend for the "Everything" package (manga book + redrawn video clip, $9.99) is built. Please update the front end in `web/index.html` of https://github.com/marinelj/moment-manga to use it. Keep the current single-file, two-language page (English and Chinese). The backend contract is in `docs/API_CHANGES.md`; it wins if this note and that file differ.

## What the backend now does

- `GET api/config` lists `"all"` in `products`, with its CNY charge in `charges.all`, only when the owner has set its price. It also returns `video_max_seconds` (15 by default).
- `POST api/orders` accepts `product=all` (same fields as `book`: `video`, optional `photo`, `product`, `lang`). If the clip is longer than `video_max_seconds`, the server answers 400 and creates no order.
- `GET api/orders/{id}`: for an `all` order, `progress` goes 0 to 30 while the book is drawn and 30 to 100 while the video is drawn. The whole job takes about 10 to 20 minutes. When `status` is `done`, `downloads.book` is the PDF and `downloads.clip` is the MP4 (`/api/orders/{id}/manga.mp4`). Both need the buyer cookie, which the browser already sends.
- If the video fails, `status` is `failed`; keep showing the existing failure message with the order number.

## Changes to make

1. "Everything" button: it already switches from "Coming soon" to "Get everything" when `all` is in `products`. Check that it then starts checkout with `product=all`, exactly like the book button does with `book`.
2. Clip length: when the chosen clip is longer than `video_max_seconds`, keep the "Everything" button disabled and show one line under it: "The redrawn clip needs a play of 15 seconds or less." (Chinese: "重绘视频需要 15 秒以内的片段。"). Read the number from the config, do not hard-code 15. The book stays available.
3. Waiting: for an `all` order, under the progress bar, say "This takes about 15 minutes. You can keep this page open." (Chinese: "大约需要 15 分钟，请保持页面打开。"). Keep polling every few seconds as now.
4. Downloads: the done state already renders `downloads.book` and `downloads.clip`. Also show the clip in a `<video controls playsinline>` player above the download buttons when `downloads.clip` is present.
5. Do not change anything about payment, amounts or cookies. The browser never sends an amount.

## Done when

- With a server whose config lists `all`, a 12-second clip can be bought as "Everything", the progress bar moves, and both files download.
- A 20-second clip can still buy the book, but not "Everything".
- The GitHub Pages preview (no backend) still shows the demo and "Coming soon".
