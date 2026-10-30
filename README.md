# url-render-service

A **stateless** URL-rendering microservice for Kubernetes: point it at an
`http(s)` URL and get back rendered **HTML**, **visible text**, a **PNG
screenshot**, or a **PDF** -- from a real headless Chromium.

Because every request runs in a fresh browser context (isolated cookies /
storage) and the contexts are torn down after the response, the service is
stateless and safe to scale behind a plain round-robin `ClusterIP` Service.

```
   your app / curl / agent
            |  POST /render {url, output}
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

| Method | Path      | Returns                                                        |
|--------|-----------|----------------------------------------------------------------|
| GET    | `/health` | `{"status":"ok","browser":"ready"}`                            |
| GET    | `/`       | service description + example request                          |
| POST   | `/render` | JSON (`html`/`text`) or binary (`screenshot`/`pdf`)            |

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

## Quickstart

```bash
# 1. Build + push the image
REGISTRY=registry.example.com/akhil ./scripts/build.sh --push

# 2. Deploy
REGISTRY=registry.example.com/akhil ./scripts/deploy.sh

# 3. Reach it (cluster-internal)
./scripts/port-forward.sh &

# 4. Use it
curl -s localhost:8000/health
curl -s -X POST localhost:8000/render -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com","output":"text"}'
curl -s -X POST localhost:8000/render -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com","output":"screenshot"}' -o shot.png

# Or use the Python helper:
python3 client/render_example.py https://example.com text
```

## Layout

```
Dockerfile                 python:3.12-slim + Playwright Chromium, non-root
requirements.txt
app/main.py                FastAPI app (health, render)
k8s/namespace.yaml         namespace: url-render
k8s/configmap.yaml         MAX_CONCURRENT
k8s/deployment.yaml        2 replicas, non-root, /dev/shm, probes
k8s/service.yaml           ClusterIP :8000
k8s/networkpolicy.yaml     egress: internet only (anti-SSRF)
k8s/hpa.yaml               optional CPU HPA (needs metrics-server)
client/render_example.py   stdlib-only client
scripts/                   build / deploy / port-forward / smoke-test
```

## Security notes

- **Anti-SSRF egress policy** blocks private / loopback / link-local /
  metadata (`169.254.0.0/16`) ranges so a malicious `url` can't be used to
  probe in-cluster services or the cloud metadata endpoint. To allow an
  internal target, add its CIDR to the egress `ipBlock`.
- Runs **non-root**, drops all capabilities, `seccompProfile: RuntimeDefault`,
  `readOnlyRootFilesystem`, bounded concurrency (`MAX_CONCURRENT`).
- On **k3s**, NetworkPolicy is only enforced if the node was started with
  `--enable-network-policy`.

## Caveats

- Bot-protected sites (Cloudflare, etc.) may challenge a headless browser;
  a custom `user_agent` sometimes helps, but treat it as best-effort.
- `pdf`/`screenshot` return binary with an `X-Render-Url` header.
