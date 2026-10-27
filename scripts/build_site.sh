#!/usr/bin/env sh
# Builds the static site: English at /, Chinese at /zh/ (same page, lang="zh-CN").
set -e
out="${1:-_site}"
rm -rf "$out" && mkdir -p "$out/zh"
cp web/index.html "$out/index.html"
cp -r web/demo "$out/demo"
sed 's/<html lang="en"/<html lang="zh-CN"/' web/index.html > "$out/zh/index.html"
grep -q 'lang="zh-CN"' "$out/zh/index.html"
