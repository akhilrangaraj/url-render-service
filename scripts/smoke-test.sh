#!/usr/bin/env bash
# Smoke-test a running (port-forwarded) renderer.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== /health =="
curl -fsS "${BASE}/health"; echo; echo

echo "== render https://example.com -> text =="
curl -fsS -X POST "${BASE}/render" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","output":"text"}' | head -c 800; echo; echo

echo "== render https://example.com -> screenshot (out/render.png) =="
curl -fsS -X POST "${BASE}/render" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","output":"screenshot","full_page":true}' \
  -o out/render.png && ls -l out/render.png && echo "OK: PNG written"

echo "== render https://example.com -> pdf (out/render.pdf) =="
curl -fsS -X POST "${BASE}/render" -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","output":"pdf"}' \
  -o out/render.pdf && ls -l out/render.pdf && echo "OK: PDF written"
