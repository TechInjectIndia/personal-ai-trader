"""One-shot dashboard screenshotter for the case-study package.

Renders the live local dashboard (127.0.0.1:8501, no auth at the socket) page by
page via headless chromium and writes PNGs into case-study/assets/. Avoids the
Summary page (broker name + Kite account id = PII).

Run:  .venv/bin/python scripts/shoot_dashboard.py
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8501"
OUT = Path(__file__).resolve().parent.parent / "case-study" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

# (url_path, out_name, full_page, settle_seconds)
SHOTS = [
    ("league", "screenshot-league.png", True, 6),
    ("improve", "screenshot-self-improvement.png", True, 6),
    ("retros", "screenshot-retros.png", True, 6),
    ("system-map", "screenshot-system-map.png", True, 8),
    ("", "screenshot-overview.png", True, 6),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--force-color-profile=srgb"])
        ctx = browser.new_context(
            viewport={"width": 1480, "height": 1000},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        for path, name, full, settle in SHOTS:
            url = f"{BASE}/{path}".rstrip("/")
            print(f"-> {url}")
            page.goto(url, wait_until="networkidle", timeout=60000)
            # Streamlit hydrates after networkidle; give widgets/plots time, then
            # nudge a scroll so lazy charts/graph iframes paint.
            time.sleep(settle)
            page.mouse.wheel(0, 1200)
            time.sleep(2)
            page.mouse.wheel(0, -4000)
            time.sleep(1)
            out = OUT / name
            page.screenshot(path=str(out), full_page=full)
            print(f"   saved {out} ({out.stat().st_size // 1024} KB)")
        browser.close()


if __name__ == "__main__":
    main()
