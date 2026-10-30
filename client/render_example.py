# Stateless URL -> {html, text, screenshot, pdf} renderer.

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"  # assumes `scripts/port-forward.sh` is running


def _post(payload: dict) -> tuple[bytes, str]:
    req = urllib.request.Request(
        BASE + "/render",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), r.headers.get("Content-Type", "")


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    output = sys.argv[2] if len(sys.argv) > 2 else "text"

    body, ctype = _post({"url": url, "output": output, "full_page": True})

    if output in ("screenshot", "pdf"):
        ext = "png" if output == "screenshot" else "pdf"
        out = f"out/{url.rstrip('/').split('/')[-1] or 'index'}.{ext}"
        with open(out, "wb") as f:
            f.write(body)
        print(f"wrote {out} ({len(body)} bytes, {ctype})")
    else:
        print(body.decode(errors="replace"))


if __name__ == "__main__":
    main()
