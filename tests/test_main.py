"""Integration tests for the URL render service.

These tests drive the real FastAPI app with a real headless Chromium (no
Playwright mocking). Config is controlled by setting env vars and then
reloading ``app.main`` -- the module reads its config at import time, which
keeps the production code path (env at import) intact and testable.
"""
from __future__ import annotations

import asyncio
import importlib
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

OK_HTML = "<html><head><title>ok</title></head><body><h1>Hello</h1><p>world</p></body></html>"

# Fires an infinite JS loop shortly after load: any later page.evaluate /
# page.content() hangs forever until the context is closed.
BUSY_HTML = (
    "<html><head><script>"
    "window.addEventListener('load', () => setTimeout(() => { for(;;); }, 300));"
    "</script></head><body>busy</body></html>"
)

BASE_ENV = {"RENDER_API_KEYS": "alice:alice-key-123,bob:bob-key-456"}
AUTH_ALICE = {"Authorization": "Bearer alice-key-123"}
AUTH_BOB = {"X-API-Key": "bob-key-456"}


@pytest.fixture(scope="session")
def http_server(tmp_path_factory):
    """Local http.server on a random 127.0.0.1 port serving the test pages."""
    root = tmp_path_factory.mktemp("www")
    (root / "ok.html").write_text(OK_HTML, encoding="utf-8")
    (root / "busy.html").write_text(BUSY_HTML, encoding="utf-8")
    (root / "ü日本.html").write_text(OK_HTML, encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _reload_main(monkeypatch, **env):
    """Set env vars (deleting the ones not listed) and reload app.main."""
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    # Don't let unrelated auth/config settings leak in from the host env.
    for name in ("RENDER_API_KEYS", "RENDER_API_KEYS_FILE", "AUTH_DISABLED", "CHROMIUM_SANDBOX"):
        if name not in env:
            monkeypatch.delenv(name, raising=False)
    import app.main as main

    importlib.reload(main)
    return main


def test_health_200_when_browser_ready(monkeypatch):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "browser": "ready"}


def test_health_503_when_browser_disconnected(monkeypatch):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200

        class _Disconnected:
            def is_connected(self) -> bool:
                return False

            async def close(self) -> None:
                return None

        main.state.browser = _Disconnected()
        r = client.get("/health")
        assert r.status_code == 503
        assert r.json() == {"status": "starting", "browser": "unavailable"}


def test_render_401_without_key(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        r = client.post("/render", json={"url": f"{http_server}/ok.html", "output": "text"})
        assert r.status_code == 401
        assert r.headers.get("www-authenticate") == "Bearer"


def test_render_401_with_wrong_key(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        r = client.post(
            "/render",
            json={"url": f"{http_server}/ok.html", "output": "text"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert r.status_code == 401
        assert r.headers.get("www-authenticate") == "Bearer"


def test_render_200_with_bearer_key(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        r = client.post(
            "/render",
            json={"url": f"{http_server}/ok.html", "output": "text"},
            headers=AUTH_ALICE,
        )
        assert r.status_code == 200


def test_render_200_with_x_api_key(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        r = client.post(
            "/render",
            json={"url": f"{http_server}/ok.html", "output": "text"},
            headers=AUTH_BOB,
        )
        assert r.status_code == 200


def test_auth_disabled_allows_anonymous(monkeypatch, http_server):
    main = _reload_main(monkeypatch, AUTH_DISABLED="1")
    with TestClient(main.app) as client:
        r = client.post("/render", json={"url": f"{http_server}/ok.html", "output": "text"})
        assert r.status_code == 200


def test_text_output_includes_status_and_final_url(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/ok.html"
        r = client.post("/render", json={"url": url, "output": "text"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == 200
        assert body["final_url"] == url
        assert "Hello" in body["text"]
        assert body["truncated"] is False
        assert body["length"] == len(body["text"])


def test_html_output_includes_status_and_final_url(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/ok.html"
        r = client.post("/render", json={"url": url, "output": "html"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == 200
        assert body["final_url"] == url
        assert "<h1>Hello</h1>" in body["html"]
        assert body["truncated"] is False


def test_404_page_reports_status_404(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/does-not-exist.html"
        r = client.post("/render", json={"url": url, "output": "text"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == 404
        assert body["final_url"] == url


def test_screenshot_returns_png(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/ok.html"
        r = client.post("/render", json={"url": url, "output": "screenshot"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        assert r.content[:8] == PNG_MAGIC
        assert r.headers.get("x-render-status") == "200"
        assert r.headers.get("x-render-url") == url


def test_pdf_returns_pdf(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/ok.html"
        r = client.post("/render", json={"url": url, "output": "pdf"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
        assert r.headers.get("x-render-status") == "200"


def test_busy_loop_page_hits_deadline_and_releases_slot(monkeypatch, http_server):
    # A single slot: if the hung render leaked it, the follow-up request below
    # would queue behind it and fail instead of returning 200.
    main = _reload_main(
        monkeypatch, **BASE_ENV, RENDER_GRACE_S="2", MAX_CONCURRENT="1", PER_USER_CONCURRENT="1"
    )
    with TestClient(main.app) as client:
        payload = {
            "url": f"{http_server}/busy.html",
            "output": "text",
            "timeout_ms": 1000,
            "wait_ms": 1000,
        }
        start = time.monotonic()
        r = client.post("/render", json=payload, headers=AUTH_ALICE)
        elapsed = time.monotonic() - start
        assert r.status_code == 504
        assert r.json()["detail"] == "render deadline exceeded"
        assert elapsed < 20  # the deadline (4s) must actually trip, not hang

        # The render slot must have been released: a normal render still works.
        r2 = client.post(
            "/render",
            json={"url": f"{http_server}/ok.html", "output": "text"},
            headers=AUTH_ALICE,
        )
        assert r2.status_code == 200
        assert r2.json()["text"]


def test_queue_overflow_returns_429(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV, MAX_CONCURRENT="1", MAX_QUEUE="0")
    with TestClient(main.app) as client:
        slow = {"url": f"{http_server}/ok.html", "output": "text", "wait_ms": 3000}
        results: dict = {}
        lock = threading.Lock()

        def worker(name: str) -> None:
            resp = client.post("/render", json=slow, headers=AUTH_ALICE)
            with lock:
                results[name] = resp

        t1 = threading.Thread(target=worker, args=("a",))
        t1.start()
        time.sleep(0.5)  # let request A grab the single slot
        t2 = threading.Thread(target=worker, args=("b",))
        t2.start()
        t1.join()
        t2.join()

        assert results["a"].status_code == 200
        assert results["b"].status_code == 429
        assert results["b"].headers.get("retry-after") == "5"


def test_non_ascii_url_screenshot_does_not_500(monkeypatch, http_server):
    main = _reload_main(monkeypatch, **BASE_ENV)
    with TestClient(main.app) as client:
        url = f"{http_server}/\u00fc\u65e5\u672c.html"
        r = client.post("/render", json={"url": url, "output": "screenshot"}, headers=AUTH_ALICE)
        assert r.status_code == 200
        assert r.content[:8] == PNG_MAGIC
        header = r.headers.get("x-render-url")
        assert header is not None
        assert "\u00fc\u65e5\u672c" not in header  # must be percent-encoded
        assert "%C3%BC%E6%97%A5%E6%9C%AC" in header


def test_app_refuses_to_start_without_keys(monkeypatch):
    main = _reload_main(monkeypatch)  # no keys, no AUTH_DISABLED

    async def run_lifespan() -> None:
        async with main.app.router.lifespan_context(main.app):
            pass

    with pytest.raises(RuntimeError, match="RENDER_API_KEYS|AUTH_DISABLED"):
        asyncio.run(run_lifespan())
