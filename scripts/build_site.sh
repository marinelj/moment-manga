#!/usr/bin/env sh
# Builds the static site: English at /, Chinese at /zh/ (same page, lang="zh-CN").
set -e
out="${1:-_site}"
rm -rf "$out" && mkdir -p "$out/zh"
cp web/index.html "$out/index.html"
cp -r web/demo "$out/demo"
mkdir -p "$out/admin" && cp app/admin.html "$out/admin/index.html"   # owner debug page; the backend serves the same file at /admin
sed 's/<html lang="en"/<html lang="zh-CN"/' web/index.html > "$out/zh/index.html"
# Empty MANGA_API_BASE keeps a standalone demo; backend-served HTML stays same-origin.
MANGA_API_BASE="${MANGA_API_BASE-https://marine-api.tunnel.tungo.cn/manga}" python3 - "$out" <<'PY'
import html
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

base = os.environ['MANGA_API_BASE'].strip().rstrip('/')
if base:
    url = urlsplit(base)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not (url.scheme == 'https' or
                    url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1'))):
        raise SystemExit('MANGA_API_BASE must be an HTTPS server base (or loopback HTTP).')
for name in ('index.html', 'zh/index.html'):
    page = Path(sys.argv[1]) / name
    page.write_text(page.read_text().replace(
        '<meta name="moment-manga-api-base" content="">',
        '<meta name="moment-manga-api-base" content="' + html.escape(base, quote=True) + '">'))
PY
grep -q 'lang="zh-CN"' "$out/zh/index.html"
