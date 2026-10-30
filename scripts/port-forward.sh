#!/usr/bin/env bash
# Port-forward the ClusterIP service to 127.0.0.1 (Ctrl-C to stop).
set -euo pipefail
NS="${NS:-browsers}"
PORT="${PORT:-8000}"
echo "Forwarding svc/url-render -> 127.0.0.1:${PORT}  (Ctrl-C to stop)"
kubectl -n "${NS}" port-forward svc/url-render "${PORT}:8000"
