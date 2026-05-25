"""Capture one expanded retrospective (the plain-English grade) for the case study."""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8501/retros"
OUT = Path(__file__).resolve().parent.parent / "case-study" / "assets" / "screenshot-retro-expanded.png"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--force-color-profile=srgb"])
        ctx = browser.new_context(viewport={"width": 1480, "height": 1100}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        time.sleep(6)
        # Streamlit expanders render as <details data-testid="stExpander"> with a
        # clickable <summary>. Open the first few review rows so the grade shows.
        summaries = page.locator('details[data-testid="stExpander"] summary')
        n = summaries.count()
        print(f"found {n} expanders")
        # The first expander is the "Run the retro cron now" panel; skip to reviews.
        opened = 0
        for i in range(n):
            txt = (summaries.nth(i).inner_text() or "").strip()
            if any(k in txt for k in ("Bad call", "Unlucky", "Lucky", "Good call", "Mixed", "·")):
                summaries.nth(i).click()
                opened += 1
                time.sleep(1.5)
                if opened >= 1:
                    break
        time.sleep(2)
        # Scroll the opened review into view near the top.
        page.mouse.wheel(0, 850)
        time.sleep(1.5)
        page.screenshot(path=str(OUT), full_page=False)
        print(f"saved {OUT} ({OUT.stat().st_size // 1024} KB)")
        browser.close()


if __name__ == "__main__":
    main()
