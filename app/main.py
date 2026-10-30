"""Stateless URL render service.

One headless Chromium is launched per pod (at startup). Every request opens a
brand-new browser context + page (isolated cookies / storage / cache), renders
the requested URL, and returns HTML / visible text / a PNG screenshot / a PDF.
The context is torn down as soon as the response is produced, so no state is
ever shared between requests -- which is what makes this safe to scale behind
a plain round-robin Service.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from playwright.async_api import Browser, Playwright, async_playwright

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "4"))

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


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
    pw: Optional[Playwright] = None
    browser: Optional[Browser] = None
    semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT)


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.pw = await async_playwright().start()
    state.browser = await state.pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-color-profile=srgb",
        ],
    )
    try:
        yield
    finally:
        if state.browser is not None:
            await state.browser.close()
        if state.pw is not None:
            await state.pw.stop()


app = FastAPI(title="URL Render Service", version="1.0.0", lifespan=lifespan)


def _browser() -> Browser:
    if state.browser is None or not state.browser.is_connected():
        raise HTTPException(status_code=503, detail="browser is not ready yet")
    return state.browser


def _validate_url(url: str) -> None:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")


@app.get("/")
async def root() -> dict:
    return {
        "service": "url-render-service",
        "endpoints": {
            "GET /health": "liveness / readiness",
            "POST /render": "render a URL to html | text | screenshot | pdf",
        },
        "example_request": {"url": "https://example.com", "output": "text"},
    }


@app.get("/health")
async def health() -> dict:
    ready = state.browser is not None and state.browser.is_connected()
    return {
        "status": "ok" if ready else "starting",
        "browser": "ready" if ready else "unavailable",
    }


@app.post("/render")
async def render(req: RenderRequest):
    browser = _browser()
    _validate_url(req.url)

    async with state.semaphore:
        context = await browser.new_context(
            viewport={"width": req.viewport_w, "height": req.viewport_h},
            user_agent=req.user_agent or DEFAULT_UA,
        )
        try:
            page = await context.new_page()
            try:
                await page.goto(req.url, timeout=req.timeout_ms, wait_until=req.wait_until)
            except Exception as exc:  # noqa: BLE001 - surfaced as a clean 502
                raise HTTPException(status_code=502, detail=f"navigation failed: {exc}")

            if req.wait_ms:
                await page.wait_for_timeout(req.wait_ms)

            if req.output == "html":
                html = await page.content()
                return {"url": req.url, "output": "html", "length": len(html), "html": html}

            if req.output == "text":
                text = await page.evaluate(
                    "() => (document.body ? document.body.innerText : '')"
                )
                text = text or ""
                return {"url": req.url, "output": "text", "length": len(text), "text": text}

            if req.output == "screenshot":
                png = await page.screenshot(full_page=req.full_page, type="png")
                return Response(
                    content=png, media_type="image/png", headers={"X-Render-Url": req.url}
                )

            if req.output == "pdf":
                pdf = await page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
                )
                return Response(
                    content=pdf, media_type="application/pdf", headers={"X-Render-Url": req.url}
                )

            raise HTTPException(status_code=400, detail="unknown output")
        finally:
            await context.close()
