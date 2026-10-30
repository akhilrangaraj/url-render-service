"""Stateless URL render service.

One headless Chromium is launched per pod (at startup). Every request opens a
brand-new browser context + page (isolated cookies / storage / cache), renders
the requested URL, and returns HTML / visible text / a PNG screenshot / a PDF.
The context is torn down as soon as the response is produced, so no state is
ever shared between requests -- which is what makes this safe to scale behind
a plain round-robin Service.

Authentication: ``POST /render`` requires an API key (``Authorization:
Bearer <key>`` or ``X-API-Key: <key>``) unless ``AUTH_DISABLED=1`` is set.
``GET /health`` and ``GET /`` stay open so kubelet probes keep working.

A hostile page cannot wedge a render slot forever: the whole per-request
render runs under a hard deadline (``asyncio.wait_for``), and the browser
context is always closed (itself bounded) in a ``finally`` -- closing the
context kills a hung renderer.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, Optional, Union
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from pydantic import BaseModel, Field

log = logging.getLogger("url-render")

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Characters allowed unquoted in a URL; anything else (notably non-latin-1
# codepoints) is percent-encoded so the X-Render-Url header stays latin-1 safe.
_URL_HEADER_SAFE = ":/?#[]@!$&'()*+,;=%"


@dataclass(frozen=True)
class Config:
    """Runtime configuration, read from the environment at import time.

    Tests control it by setting env vars and then ``importlib.reload``-ing
    this module, which re-runs ``load_config()``.
    """

    max_concurrent: int
    max_queue: int
    per_user_concurrent: int
    render_grace_s: float
    max_chars: int
    max_screenshot_height: int
    chromium_sandbox: bool
    auth_disabled: bool
    api_keys: dict[str, str] = field(compare=False)  # user -> key


def _parse_api_key_pairs(raw: str) -> dict[str, str]:
    """Parse ``user:key,user:key`` into {user: key}; raise on bad entries."""
    pairs: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        user, sep, key = part.partition(":")
        user, key = user.strip(), key.strip()
        if not sep or not user or not key:
            raise ValueError(f"invalid API key entry {part!r}: expected 'user:key'")
        pairs[user] = key
    return pairs


def load_config() -> Config:
    """Build :class:`Config` from the environment.

    Both ``RENDER_API_KEYS`` and ``RENDER_API_KEYS_FILE`` may be set; file
    entries are loaded first, then env entries override per user.
    """
    api_keys: dict[str, str] = {}
    keys_file = os.environ.get("RENDER_API_KEYS_FILE")
    if keys_file:
        with open(keys_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                api_keys.update(_parse_api_key_pairs(line))
    if os.environ.get("RENDER_API_KEYS"):
        api_keys.update(_parse_api_key_pairs(os.environ["RENDER_API_KEYS"]))
    return Config(
        max_concurrent=int(os.environ.get("MAX_CONCURRENT", "4")),
        max_queue=int(os.environ.get("MAX_QUEUE", "16")),
        per_user_concurrent=int(os.environ.get("PER_USER_CONCURRENT", "2")),
        render_grace_s=float(os.environ.get("RENDER_GRACE_S", "20")),
        max_chars=int(os.environ.get("MAX_CHARS", "5000000")),
        max_screenshot_height=int(os.environ.get("MAX_SCREENSHOT_HEIGHT", "16384")),
        chromium_sandbox=os.environ.get("CHROMIUM_SANDBOX") == "1",
        auth_disabled=os.environ.get("AUTH_DISABLED") == "1",
        api_keys=api_keys,
    )


config = load_config()


class RenderRequest(BaseModel):
    url: str
    output: Literal["html", "text", "screenshot", "pdf"] = "html"
    full_page: bool = Field(False, description="screenshot: capture full scroll height")
    wait_ms: int = Field(0, ge=0, le=60_000, description="Extra settle time after load")
    viewport_w: int = Field(1280, ge=320, le=3840)
    viewport_h: int = Field(720, ge=240, le=2160)
    timeout_ms: int = Field(30_000, ge=1_000, le=120_000, description="Navigation timeout")
    wait_until: Literal["commit", "load", "domcontentloaded", "networkidle"] = "load"
    user_agent: Optional[str] = Field(None, description="Override the default user agent")


class _State:
    """Per-process shared state: one browser, bounded by semaphores."""

    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.semaphore = asyncio.Semaphore(config.max_concurrent)
        self.user_semaphores: dict[str, asyncio.Semaphore] = {}
        # Requests (active + waiting) currently inside the render handler.
        # Single event loop: no lock needed.
        self.inflight: int = 0


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.auth_disabled and not config.api_keys:
        raise RuntimeError(
            "refusing to start: no API keys configured. Set RENDER_API_KEYS "
            "(comma-separated 'user:key' pairs) or RENDER_API_KEYS_FILE, or set "
            "AUTH_DISABLED=1 for local development only."
        )
    if config.auth_disabled:
        log.warning(
            "AUTH_DISABLED=1 is set -- POST /render has NO authentication. "
            "Local development only; never run this in production."
        )
    args = [
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
    ]
    if not config.chromium_sandbox:
        # Default: --no-sandbox. The container (non-root, dropped caps,
        # seccomp, read-only root fs) is the isolation boundary; Chromium's
        # own sandbox adds little value inside a container. Set
        # CHROMIUM_SANDBOX=1 to keep it.
        args.insert(0, "--no-sandbox")
    state.pw = await async_playwright().start()
    state.browser = await state.pw.chromium.launch(headless=True, args=args)
    try:
        yield
    finally:
        if state.browser is not None:
            await state.browser.close()
        if state.pw is not None:
            await state.pw.stop()


app = FastAPI(title="URL Render Service", version="1.0.0", lifespan=lifespan)


def _extract_key(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    """Pull the candidate API key out of the auth headers (Bearer wins)."""
    if authorization is not None:
        scheme, sep, token = authorization.partition(" ")
        if sep and scheme.lower() == "bearer" and token.strip():
            return token.strip()
    if x_api_key is not None and x_api_key.strip():
        return x_api_key.strip()
    return None


def require_api_key(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """Resolve the calling user from the request's API key (FastAPI dependency).

    Returns ``"anonymous"`` when auth is disabled. Raises 401 with
    ``WWW-Authenticate: Bearer`` when the key is missing or unknown.
    """
    if config.auth_disabled:
        return "anonymous"
    key = _extract_key(authorization, x_api_key)
    if key is None:
        raise HTTPException(
            status_code=401,
            detail="missing API key (send 'Authorization: Bearer <key>' or 'X-API-Key: <key>')",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Compare fixed-length digests so neither timing nor length leaks the key;
    # every key is checked, no early exit.
    presented = hashlib.sha256(key.encode("utf-8")).digest()
    matched: Optional[str] = None
    for user, expected in config.api_keys.items():
        if hmac.compare_digest(presented, hashlib.sha256(expected.encode("utf-8")).digest()):
            matched = user
    if matched is None:
        raise HTTPException(
            status_code=401, detail="invalid API key", headers={"WWW-Authenticate": "Bearer"}
        )
    return matched


def _browser_ready() -> bool:
    """True when the shared browser exists and is connected."""
    browser = state.browser
    if browser is None:
        return False
    connected = browser.is_connected  # a method in the Python API
    if callable(connected):  # tolerate stubs that expose it as a plain attribute
        connected = connected()
    return bool(connected)


def _browser() -> Browser:
    if not _browser_ready():
        raise HTTPException(status_code=503, detail="browser is not ready yet")
    return state.browser


def _validate_url(url: str) -> None:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")


def _user_semaphore(user: str) -> asyncio.Semaphore:
    """Per-user fairness semaphore (lazy-created; one per distinct user)."""
    sem = state.user_semaphores.get(user)
    if sem is None:
        sem = asyncio.Semaphore(config.per_user_concurrent)
        state.user_semaphores[user] = sem
    return sem


def _first_line(message: str, limit: int = 300) -> str:
    """First line of an error message, truncated -- safe for API details."""
    lines = message.splitlines()
    return (lines[0] if lines else "")[:limit]


def _deadline_s(req: RenderRequest) -> float:
    """Hard per-request render deadline (seconds): nav + settle + grace."""
    return req.timeout_ms / 1000 + req.wait_ms / 1000 + config.render_grace_s


def _encode_url_header(url: str) -> str:
    """Percent-encode a URL so it is safe in a latin-1 header (RFC 3986)."""
    return quote(url, safe=_URL_HEADER_SAFE)


def _binary_headers(req: RenderRequest, status: Optional[int]) -> dict[str, str]:
    headers = {"X-Render-Url": _encode_url_header(req.url)}
    if status is not None:
        headers["X-Render-Status"] = str(status)
    return headers


def _truncated(content: str) -> tuple[str, bool]:
    """Cap text output at MAX_CHARS; return (content, was_truncated)."""
    if len(content) > config.max_chars:
        return content[: config.max_chars], True
    return content, False


async def _render_once(
    browser: Browser, req: RenderRequest, ctx_box: dict[str, Optional[BrowserContext]]
) -> Union[dict, Response]:
    """Open a fresh context + page, render, and build the response.

    The created context is recorded in ``ctx_box`` so the caller can close it
    in a ``finally`` even when this coroutine is cancelled by the deadline.
    """
    context = await browser.new_context(
        viewport={"width": req.viewport_w, "height": req.viewport_h},
        user_agent=req.user_agent or DEFAULT_UA,
    )
    ctx_box["context"] = context
    page = await context.new_page()

    status: Optional[int] = None
    try:
        response = await page.goto(req.url, timeout=req.timeout_ms, wait_until=req.wait_until)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean 502
        raise HTTPException(status_code=502, detail=f"navigation failed: {_first_line(str(exc))}") from None
    if response is not None:
        status = response.status

    if req.wait_ms:
        await page.wait_for_timeout(req.wait_ms)

    final_url = page.url

    if req.output == "html":
        html, truncated = _truncated(await page.content())
        return {
            "url": req.url,
            "output": "html",
            "status": status,
            "final_url": final_url,
            "length": len(html),
            "truncated": truncated,
            "html": html,
        }

    if req.output == "text":
        text = await page.evaluate("() => (document.body ? document.body.innerText : '')")
        text, truncated = _truncated(text or "")
        return {
            "url": req.url,
            "output": "text",
            "status": status,
            "final_url": final_url,
            "length": len(text),
            "truncated": truncated,
            "text": text,
        }

    if req.output == "screenshot":
        if req.full_page:
            height = await page.evaluate("() => document.documentElement.scrollHeight")
            if height is not None and height > config.max_screenshot_height:
                # Cap extremely tall pages: clip instead of full_page.
                png = await page.screenshot(
                    type="png",
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": req.viewport_w,
                        "height": config.max_screenshot_height,
                    },
                )
            else:
                png = await page.screenshot(type="png", full_page=True)
        else:
            png = await page.screenshot(type="png")
        return Response(content=png, media_type="image/png", headers=_binary_headers(req, status))

    if req.output == "pdf":
        pdf = await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
        )
        return Response(content=pdf, media_type="application/pdf", headers=_binary_headers(req, status))

    raise HTTPException(status_code=400, detail="unknown output")


@app.get("/")
async def root() -> dict:
    return {
        "service": "url-render-service",
        "endpoints": {
            "GET /health": "liveness / readiness",
            "POST /render": "render a URL to html | text | screenshot | pdf (API key required)",
        },
        "example_request": {"url": "https://example.com", "output": "text"},
    }


@app.get("/health")
async def health():
    """Liveness/readiness. 503 while the browser is not connected, so a
    crashed browser trips the liveness probe and the pod gets restarted."""
    ready = _browser_ready()
    body = {
        "status": "ok" if ready else "starting",
        "browser": "ready" if ready else "unavailable",
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.post("/render")
async def render(req: RenderRequest, user: str = Depends(require_api_key)):
    browser = _browser()
    _validate_url(req.url)

    limit = config.max_concurrent + config.max_queue
    if state.inflight >= limit:
        raise HTTPException(
            status_code=429,
            detail="too many renders in flight; retry shortly",
            headers={"Retry-After": "5"},
        )
    state.inflight += 1
    ctx_box: dict[str, Optional[BrowserContext]] = {}
    try:
        # Per-user first (fairness), then global capacity.
        async with _user_semaphore(user), state.semaphore:
            try:
                return await asyncio.wait_for(_render_once(browser, req, ctx_box), timeout=_deadline_s(req))
            except asyncio.TimeoutError:
                log.warning("render deadline exceeded: url=%s", req.url)
                raise HTTPException(status_code=504, detail="render deadline exceeded") from None
    finally:
        state.inflight -= 1
        context = ctx_box.get("context")
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=10)
            except Exception:  # noqa: BLE001 - closing must never fail the response
                log.warning("error closing browser context", exc_info=True)
