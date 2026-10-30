# url-render-service

A **stateless** URL-rendering microservice for Kubernetes: point it at an
`http(s)` URL and get back rendered **HTML**, **visible text**, a **PNG
screenshot**, or a **PDF** -- from a real headless Chromium.

Because every request runs in a fresh browser context (isolated cookies /
storage) and the contexts are torn down after the response, the service is
stateless and safe to scale behind a plain round-robin `ClusterIP` Service.

```\
   your app / curl / agent
            |  POST /render {url, output}   (Authorization: Bearer <key>)
            v
   +------------------------------------------+
   |  url-render (Deployment, N replicas)     |
   |  FastAPI + headless Chromium (Playwright)|
   |  1 browser/pod, 1 context/request         |
   +------------------------------------------+
            |  egress: internet only (NetworkPolicy, anti-SSRF)
            v
         the public web
```

## API

| Method | Path      | Auth   | Returns                                                        |
|--------|-----------|--------|----------------------------------------------------------------|
| GET    | `/health` | none   | `200 {"status":"ok","browser":"ready"}` / `503 {"status":"starting",...}` |
| GET    | `/`       | none   | service description + example request                          |
| POST   | `/render` | API key| JSON (`html`/`text`) or binary (`screenshot`/`pdf`)            |

`POST /render` body:

```json
{
  "url": "https://example.com",
  "output": "text",                 // html | text | screenshot | pdf
  "full_page": false,               // screenshot only
  "wait_ms": 0,                     // extra settle time after load
  "viewport_w": 1280, "viewport_h": 720,
  "timeout_ms": 30000,
  "wait_until": "load",             // commit | load | domcontentloaded | networkidle
  "user_agent": null
}
```

Response fields:

- JSON outputs (`html`, `text`) include `"status"` (HTTP status of the main
  navigation response, or `null`), `"final_url"` (the page URL after
  navigation), `"length"` (delivered), and `"truncated"` (`true` when the
  output was capped at `MAX_CHARS`).
- Binary outputs (`screenshot`, `pdf`) carry `X-Render-Url` (percent-encoded
  so it is latin-1 header-safe) and `X-Render-Status` headers.
- `504 {"detail":"render deadline exceeded"}` when a page hangs (busy-loop,
  stalled navigation); the render slot is always released.
- `429` with `Retry-After: 5` when active + queued renders hit
  `MAX_CONCURRENT + MAX_QUEUE`.

## Authentication

`POST /render` requires an API key. Keys are configured as `user:key` pairs
via `RENDER_API_KEYS` (comma-separated, e.g. `alice:abc,bob:def`) and/or
`RENDER_API_KEYS_FILE` (one `user:key` per line, `#` comments allowed).
**The app refuses to start with no keys** unless `AUTH_DISABLED=1` (local
dev/tests only).

Send your key with either header:

```bash
curl -s -X POST localhost:8000/render \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer abc" \
     -d '{"url":"https://example.com","output":"text"}'
# or: -H "X-API-Key: abc"
```

Keys are compared with `hmac.compare_digest` (constant-time, every key
checked). Per-user fairness: at most `PER_USER_CONCURRENT` renders run at
once per user, in addition to the global `MAX_CONCURRENT` cap, so one user
cannot occupy every slot.

## Environment variables

| Variable                | Default | Purpose                                                        |
|-------------------------|---------|----------------------------------------------------------------|
| `MAX_CONCURRENT`        | `4`     | global render slots per pod                                    |
| `MAX_QUEUE`             | `16`    | extra queued requests allowed before `429`                     |
| `PER_USER_CONCURRENT`   | `2`     | max concurrent renders per API-key user                        |
| `RENDER_GRACE_S`        | `20`    | grace (s) beyond `timeout_ms + wait_ms` before `504` deadline  |
| `MAX_CHARS`             | `5000000` | cap for `html`/`text` output (`"truncated": true` when cut) |
| `MAX_SCREENSHOT_HEIGHT` | `16384` | clip `full_page` screenshots taller than this                  |
| `CHROMIUM_SANDBOX`      | unset   | `1` = keep Chromium's `--sandbox` (omit `--no-sandbox`)        |
| `RENDER_API_KEYS`       | —       | `user:key,user:key` (see Authentication)                       |
| `RENDER_API_KEYS_FILE`  | —       | path to a `user:key` file; env entries override per user       |
| `AUTH_DISABLED`         | unset   | `1` = no auth on `/render` (local dev/tests only)              |

## Quickstart

```bash
# 1. Build + push the image (default registry: registry.rangaraj.net/akhilrangaraj)
./scripts/build.sh --push

# 2. Deploy (creates the url-render-auth secret with a generated key if absent)
./scripts/deploy.sh
# read the generated key back:
kubectl -n url-render get secret url-render-auth -o jsonpath='{.data.api-keys}' | base64 -d

# 3. Reach it (cluster-internal)
./scripts/port-forward.sh &

# 4. Use it
export RENDER_API_KEY=alice:<your-key>
curl -s localhost:8000/health
curl -s -X POST localhost:8000/render -H 'Content-Type: application/json' \
     -H "Authorization: Bearer ${RENDER_API_KEY}" \
     -d '{"url":"https://example.com","output":"text"}'

# Or use the Python helper (sends the key from $RENDER_API_KEY, writes to out/):
python3 client/render_example.py https://example.com text

# Smoke-test everything:
./scripts/smoke-test.sh
```

## CI

`.gitea/workflows/build-and-push-image.yaml` builds and pushes the image on
every push/tag to `registry.rangaraj.net/akhilrangaraj/url-render-service`
tagged by short SHA and ref slug; `latest` is updated from the default
branch.

## Layout

```
Dockerfile                 python:3.12-slim + Playwright Chromium, non-root
requirements.txt           runtime deps
requirements-dev.txt       pytest, httpx (tests)
app/main.py                FastAPI app (health, render, auth)
tests/test_main.py         integration tests (real Chromium, no mocks)
k8s/namespace.yaml         namespace: url-render
k8s/configmap.yaml         MAX_CONCURRENT
k8s/deployment.yaml        2 replicas, non-root, /dev/shm + /tmp, probes
k8s/service.yaml           ClusterIP :8000
k8s/networkpolicy.yaml     egress (anti-SSRF) + ingress allow-list
k8s/secret.example.yaml    url-render-auth example (never commit real keys)
k8s/hpa.yaml               optional CPU HPA (needs metrics-server)
client/render_example.py   stdlib-only client
scripts/                   build / deploy / port-forward / smoke-test
.gitea/workflows/          build-and-push-image CI
```

## Security notes

- **Anti-SSRF egress policy** blocks private / link-local / metadata
  (`169.254.0.0/16`) ranges so a malicious `url` can't be used to probe
  in-cluster services or the cloud metadata endpoint. **Loopback is NOT
  covered by NetworkPolicy** -- CNI plugins never see packets on `lo`; the
  `127.0.0.0/8` `except` entry is best-effort only. To allow an internal
  target, add its CIDR to the egress `ipBlock`.
- **k3s enforces NetworkPolicy by default** via its embedded kube-router
  controller (disabled only with `--disable-network-policy`); no Calico
  needed.
- Ingress is restricted to pods labelled `render-client: "true"` (any
  namespace) on TCP 8000. `kubectl port-forward` is unaffected (it enters
  the pod via the kubelet).
- Runs **non-root**, drops all capabilities, `seccompProfile:
  RuntimeDefault`, `readOnlyRootFilesystem` (writable `/tmp` + `/dev/shm`
  mounts, `HOME=/tmp`), bounded concurrency and queue
  (`MAX_CONCURRENT`, `MAX_QUEUE`), per-request hard deadline (`504`), API-key
  auth with per-user fairness.
- **`--no-sandbox` trade-off:** the container is the isolation boundary, so
  Chromium's own sandbox is off by default (`CHROMIUM_SANDBOX=1` to keep
  it). The cost: an exploit in the renderer **controls the shared browser
  process and can see other in-flight requests** in the same pod. If that
  is unacceptable, run one browser per request or isolate users at the pod
  level instead.

## Caveats

- Bot-protected sites (Cloudflare, etc.) may challenge a headless browser;
  a custom `user_agent` sometimes helps, but treat it as best-effort.
- `pdf`/`screenshot` return binary with `X-Render-Url` / `X-Render-Status`
  headers.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q tests
```
