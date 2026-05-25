"""Render the print HTML to a single Upwork-ready PDF via headless chromium.

Loads case-study/helm-case-study-print.html from disk (so relative asset paths +
the mermaid CDN both resolve), waits for mermaid to finish drawing, then prints
to A4 PDF with backgrounds on.

Run:  .venv/bin/python scripts/render_case_study_pdf.py
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "case-study" / "helm-case-study-print.html"
PDF = ROOT / "case-study" / "Helm-Case-Study-TechInject.pdf"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--force-color-profile=srgb"])
        page = browser.new_context(device_scale_factor=2).new_page()
        page.goto(HTML.as_uri(), wait_until="networkidle", timeout=60000)
        # Wait for the mermaid module to signal completion.
        page.wait_for_function("window.__mmdDone === true", timeout=45000)
        if page.evaluate("window.__mmdErr || ''"):
            print("mermaid warning:", page.evaluate("window.__mmdErr"))
        svgs = page.locator(".mermaid svg").count()
        print(f"mermaid diagrams rendered: {svgs}")
        time.sleep(1.5)  # let fonts/layout settle
        page.emulate_media(media="screen")  # keep the on-screen colors in the PDF
        page.pdf(
            path=str(PDF),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        browser.close()
    print(f"saved {PDF} ({PDF.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
