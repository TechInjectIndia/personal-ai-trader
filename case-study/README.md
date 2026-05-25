# Helm — Case Study Package

Lead-generation case study for the Helm platform, in formats that go straight onto the TechInject website **and** into Upwork proposals.

> **Positioning:** hero = *autonomous AI agent systems* (the differentiator), bridging to the broad *custom-AI-backend* wedges (lead-qual, support triage, document review, no-code rescue). Optimized for SEO **and** AEO (answer-engine quoting via FAQ/Article JSON-LD).

## Files

| File | Use it for |
|---|---|
| `Helm-Case-Study-TechInject.pdf` | **Upwork upload (primary).** Single self-contained 7-page PDF — full narrative + live-rendered diagrams + real dashboard screenshots. Regenerate with `python scripts/render_case_study_pdf.py` after editing `helm-case-study-print.html`. |
| `helm-case-study-print.html` | **PDF source.** Print-optimized, light-theme HTML (A4) that the PDF is rendered from via headless chromium. |
| `index.html` | **Website.** Self-contained landing page — embedded CSS, SEO meta + Open Graph + Twitter cards, JSON-LD `TechArticle` + `FAQPage`. Diagrams render as **live Mermaid**; app screenshots wired to the captures in `assets/`. |
| `helm-case-study.md` | **CMS / blog.** Same content as Markdown with fenced ` ```mermaid ` diagram blocks (GitHub, GitLab, Notion, most CMSs render these natively). |
| `upwork-entry.md` | **Upwork text.** The 400–600-word PORTFOLIO ENTRY block + IMAGE ASSETS list, ready to paste. Upwork can't render Mermaid, so it uses the PNG diagram exports below. |
| `assets/diagrams/*.mmd` | **Mermaid source** for the four diagrams (single source of truth). |
| `assets/0X-*.png` | **Rendered PNG** of each diagram — raster copies for Upwork and as a fallback anywhere Mermaid can't render. |

## Diagrams

| Mermaid source | PNG export | Shows |
|---|---|---|
| `assets/diagrams/04-platform-overview.mmd` | `assets/04-platform-overview.png` | Three AI agent layers on one Postgres backend (the "how it fits together" map) |
| `assets/diagrams/01-decision-pipeline.mmd` | `assets/01-decision-pipeline.png` | Layer 1 — the LLM-gated decision loop |
| `assets/diagrams/02-self-improvement-loop.mmd` | `assets/02-self-improvement-loop.png` | Layer 2 — the PM → Engineer → Tester loop |
| `assets/diagrams/03-competition-league.mmd` | `assets/03-competition-league.png` | Layer 3 — the 6-backend competition league |

To re-export PNGs after editing a `.mmd`: paste it into [mermaid.live](https://mermaid.live) and download, or run `mmdc -i file.mmd -o file.png` (mermaid-cli).

## Screenshots (captured — no PII)

Captured live from the dashboard via `scripts/shoot_dashboard.py` (the Summary page was deliberately excluded — it shows the broker name + Kite account ID):

- `assets/screenshot-hero.png` / `screenshot-league.png` — **Competition League**: six AI agents, personas, status, per-agent teams (the hero / cover).
- `assets/screenshot-self-improvement.png` — **Self-Improvement Loop**: per-agent dropdown, goal progress, queues (Layer 2 proof).
- `assets/screenshot-retro-expanded.png` — one **expanded retrospective**: the full plain-English grade (highest-impact sample output).
- `assets/screenshot-retros.png` — **Retrospectives**: the five-way verdict distribution.
- `assets/screenshot-system-map.png` — **System Map**: the codebase as a live knowledge graph.

> Heads-up: the **Summary / home** page shows the broker's real name + Kite account ID — it was deliberately excluded from all captures. Redact it if you ever shoot it.

To re-capture screenshots after a UI change: `python scripts/shoot_dashboard.py` (+ `scripts/shoot_retro_expanded.py` for the expanded retro). To rebuild the Upwork PDF: `python scripts/render_case_study_pdf.py`.

## To publish on the website

1. Upload `index.html` and the `assets/` folder together.
2. **Mermaid** loads from the jsDelivr CDN (one `<script type="module">` at the bottom). If your site's CSP blocks CDNs, self-host `mermaid.esm.min.mjs` and update that import.
3. Screenshots are already wired (`assets/screenshot-*.png`); re-run the capture script after a UI change.
4. Replace the three placeholder URLs (`canonical`, `og:image`, `og:url`) with your real published path.
5. Replace or delete the **CTA SLOT** block (clearly marked in both `index.html` and `helm-case-study.md`) — wire it to your existing contact form / booking widget.
6. Keep the footer disclaimer (research/paper-trading, not investment advice).

## Source of truth

Grounded in the master PRD at [`../docs/prd/helm-platform-prd.md`](../docs/prd/helm-platform-prd.md). Every claim traces to shipped code; no metrics were invented (operational facts and scope only).
