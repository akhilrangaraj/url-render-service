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

# 2. Deploy into namespace `browsers` (override with NS=...). Needs an image
#    pull secret `regcred` in that namespace; deploy.sh stops with a copy
#    command if it's missing. Creates the url-render-auth secret with a
#    generated key if absent.
./scripts/deploy.sh
# read the generated key back (stored as `user:key`; clients send only the key):
kubectl -n browsers get secret url-render-auth -o jsonpath='{.data.api-keys}' | base64 -d

# 3. Reach it (cluster-internal)
./scripts/port-forward.sh &

# 4. Use it
export RENDER_API_KEY=<key part only, without "user:">
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
k8s/namespace.yaml         namespace: browsers (shared with firefox-mcp-server)
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

## How this repo was built (a small benchmark)

This service was an experiment in letting a local model write production-ish
infrastructure code, with a frontier model auditing the result. All dates are
2026-10-29.

- **Builder:** `qwen3.8-27b` (Q4_K_M, llama.cpp on one Intel Arc B70),
  driven by [Hermes Agent](https://github.com/NousResearch/hermes-agent).
- **Reviewer:** `claude-opus-5-5` in Claude Code.

### 1. First version: qwen, unsupervised

One Hermes conversation built this repo and its sibling,
`firefox-mcp-server` (a stateful Firefox MCP server), from a single prompt.
The result read well: a clean FastAPI app, sensible k8s hardening and honest
comments. It had no tests.

### 2. Audit: Claude

Claude read every file without running any of qwen's code. It checked claims
about upstream behaviour against the published package source. Findings for
this repo:

| # | Finding | Severity |
|---|---------|----------|
| 1 | `readOnlyRootFilesystem: true` with no writable `/tmp`: Playwright can't create its profile dir, so the pod crash-loops on k8s (it works under plain `docker run`). | Blocker |
| 2 | A page that busy-loops after `load` makes `page.evaluate`/`page.content()` hang forever. The semaphore slot is never released, so 4 such URLs block the pod for good. | High |
| 3 | `/health` returned HTTP 200 even with the browser dead, so the liveness probe never restarted the pod. | High |
| 4 | No auth and no ingress policy: any pod in the cluster could use the renderer. | Medium |
| 5 | `--no-sandbox` with arbitrary URLs: a renderer exploit owns the shared browser and can see other requests in flight. | Medium (documented trade-off) |
| 6 | The NetworkPolicy's loopback rule did nothing (NetworkPolicy never applies to `lo`). The README said k3s needs `--enable-network-policy` and "pulls in Calico"; in fact k3s enforces NetworkPolicy by default via kube-router. | Low (docs) |
| 7 | A non-ASCII URL in `X-Render-Url` caused a 500 (latin-1 header encoding). The target's HTTP status was never reported, and output size was unbounded. | Low |
| 8 | The smoke test and the example client wrote to a missing `out/` dir. | Low |

### 3. Fix: qwen, from a Claude-written spec

Claude turned the audit into a precise spec (files, behaviour, acceptance
criteria, and a required real-Chromium test suite). It handed the spec to qwen
through `qwen-task`, a one-shot Hermes run. qwen fixed all 12 spec items and
wrote 16 integration tests: a local `http.server` serves fixture pages to a
real Chromium, with no Playwright mocks. It also reported honestly what it
assumed and what it didn't run.

The run survived a stale `kubectl port-forward` to the model server, which
was restarted mid-run. Hermes's retry logic picked up afterwards without
losing work.

### 4. Review and final tweaks: Claude

Claude reviewed the diff, re-ran the tests (16 passed) and changed four
things:

- The deadline test ran with 4 render slots, so it couldn't catch a leaked
  slot. It now runs with `MAX_CONCURRENT=1` and `PER_USER_CONCURRENT=1`, so a
  leak makes the follow-up request fail.
- API keys are compared as SHA-256 digests with `hmac.compare_digest`. Comparing
  the raw strings lets timing reveal the key's length.
- `deploy.sh` pipes the generated key into `kubectl` through stdin, so it never
  shows up in `ps`.
- Corrected a wrong comment about `Browser.is_connected`.

### Token usage

**qwen3.8-27b** (self-hosted, $0). "Input" counts every prompt token sent;
most of it was served from llama.cpp's prompt cache.

| Phase | Calls | Input | of which cached | Output | Time |
|-------|------:|------:|----------------:|-------:|-----:|
| First version, both repos together (one interleaved conversation turn) | 31 | 3,244,200 | 2,814,566 | 76,003 | 83 min |
| Hermes background "update the skill library" review, run automatically after the build | 6 | 979,492 | 955,326 | 19,013 | 24 min |
| Fix run, this repo only | 30 (+1 title) | 1,684,850 (+517) | 1,653,311 | 49,528 (+13) | 43 min |

**claude-opus-5-5** (one Claude Code session, all phases through the commit
of this section).

| Phase | Applies to | Calls | Uncached input | Cache write | Cache read | Output |
|-------|------------|------:|---------------:|------------:|-----------:|-------:|
| Audit (incl. reading upstream npm sources) | both repos | 16 | 32 | 73,297 | 1,064,684 | 21,290 |
| Multi-user / parallelism design advice | both repos | 1 | 4 | 8,122 | 89,295 | 3,332 |
| Investigating the Hermes background review | Hermes | 17 | 40 | 25,376 | 1,922,027 | 6,593 |
| Skill rewrite, both fix specs, qwen supervision | both repos | 38 | 86 | 65,566 | 6,110,424 | 38,855 |
| Review, tweaks and commit of this repo's fixes | this repo | 13 | 28 | 26,396 | 2,659,934 | 8,135 |
| Push, llama OOM diagnosis, ingress coordination | infra | 39 | 98 | 34,989 | 9,061,759 | 20,522 |
| Writing this section | this repo | 5 | 12 | 5,484 | 1,262,269 | 3,667 |
| **Total** | | **129** | **300** | **239,230** | **22,170,392** | **102,394** |

### 5. Confirmed by running the image

The audit was done by reading code alone. After the fixes, the image was built
and run with `docker run --read-only` as uid 1000:

- With the **original** setup (read-only root, no writable `/tmp`), the
  container exits at startup: `BrowserType.launch: EROFS: read-only file
  system, mkdtemp '/tmp/playwright-artifacts-…'`. That is audit finding 1,
  now observed rather than inferred.
- With the **fixed** setup (`/tmp` writable, `HOME=/tmp`), `/health` returns
  200, `/render` without a key returns 401, and text and PDF renders of
  https://example.com succeed.

The sibling repo got the same treatment, and it found a bug there that
reading the code had missed (see its README).

### Takeaways

- qwen wrote plausible, well-structured code and described upstream APIs
  accurately. It missed runtime failure modes (read-only filesystem, hostile
  pages, health semantics) and wrote no tests.
- Given a precise spec, the same model fixed everything and wrote a real
  test suite. The spec and the review are where the frontier model added the
  most value. Claude wrote about 102k output tokens in total; qwen wrote
  about 125k for the code itself (build plus fix).
- Hermes's automatic skill-writing review ran for 24 minutes after the build
  and saved several of the audit's mistakes as rules for future sessions. That
  skill was later corrected by hand, and automatic skill creation was turned
  off.
