# Case Study Prompt — Upwork Portfolio Piece

You are writing an **Upwork portfolio case study** for the project in the current working directory. The output is a single portfolio entry that will be pasted into the freelancer's Upwork profile to win AI/automation/custom-web-app work.

This prompt is project-agnostic. Run it from inside any repo. Read the repo first, then write the case study.

---

## 1. Who you are writing for

The reader is a non-technical or semi-technical buyer on Upwork scanning a freelancer's profile for ~10 seconds before deciding to click. They are not your peers. They are looking for:

- Proof you have shipped something real (not a tutorial clone).
- Proof you solved a *business* problem, not just a coding puzzle.
- Proof the same pattern can be applied to *their* business.

The positioning to land on every time: **"real backends, real databases, AI wired into actual operations — not no-code prototypes that fall over in production."**

---

## 2. What to read before writing

Inspect the repo to ground every claim in actual code. At minimum:

1. `README.md`, `CLAUDE.md`, `WORKAROUNDS.md`, or any `docs/` folder.
2. `package.json` / `pyproject.toml` / `requirements.txt` / `go.mod` — extract the real stack.
3. Entry points: `main.*`, `app.*`, `index.*`, `scripts/`, `cmd/`, cron configs, `ecosystem.config.js`, Dockerfiles, deployment configs.
4. Schema files (`schema.sql`, migrations, ORM models) — to describe the data model in one line.
5. `git log --oneline -50` — to understand what was actually built vs. planned.
6. Any LLM/API integration points (`anthropic`, `openai`, `langchain`, vector DB clients, webhook handlers, n8n workflows).
7. Test files / CI configs — signals of production-readiness to mention.

If something in the README contradicts the code, trust the code. Note the contradiction silently and write from current reality.

**Do not invent metrics.** If you cannot find a real number (latency, throughput, cost saved, hours saved, uptime, reviews), do not make one up. Use directional language ("near-zero downtime since launch", "runs unattended on cron") or omit the line.

---

## 3. Output format — one integrated narrative

The case study is **one piece**, not three. Do NOT split it into top-level sections like `## Production`, `## Features`, and `## Case Study`. Production realities (where it runs, how it's deployed, scale facts) live inside **Result**. Notable features and trust signals live inside **Solution**. Minor features are dropped, not promoted to their own section.

The output is a single `PORTFOLIO ENTRY` block using the Problem / Solution / Result framing below, followed by a single `IMAGE ASSETS` block. Nothing else. No prose before, between, or after — and no extra Markdown headings.

Paste exactly this structure:

```
Title: <Outcome-led title, max ~70 chars. Lead with the *capability*, not the tech.>

Cover image suggestion: <One line. Either an architecture diagram (Excalidraw, 10 min) or a real screenshot. Be specific about what to show.>

Problem:
<2–4 sentences. The buyer's pain in business terms. What was broken / missing / manual / fragile / expensive before this project existed. Name the user / role / team affected.>

Solution:
<3–6 sentences OR a numbered list of 3–5 steps. The architecture in plain English. Name the key integration points (LLM API, CRM, webhook, DB, scheduler). Mention any non-obvious engineering choices (idempotency, dedup, rate limits, fallbacks, cost controls) — these are the trust signals.>

Result:
<2–4 sentences. Outcomes only. Prefer numbers. If no numbers, use concrete operational facts ("runs every minute on cron unattended", "zero post-launch incidents", "verified 5.0 review", "shipped in N days"). Close with a one-line reusability hook: "Same architecture works for <adjacent use case>." >

Stack: <Comma-separated, ordered by buyer-relevance. Lead with the AI/automation pieces (Claude/OpenAI, n8n, Python, Node), then DB, then framework, then host. Drop anything generic like HTML/CSS/JavaScript.>

Live demo: <Loom link OR "Loom walkthrough available on request" OR omit if not applicable.>
Repo: <Public GitHub URL OR "Private — code walkthrough on request" OR omit.>
```

Output the case study as plain text inside a single fenced block tagged `PORTFOLIO ENTRY` so the user can copy-paste it directly. Do not add commentary before or after the block unless the user asks.

---

## 4. Writing rules (non-negotiable)

- **Target 400–600 words for the whole entry.** Enough to integrate production facts and the standout features into Solution and Result. If you find yourself going past 600, you are dumping the architecture instead of choosing what matters. Cut a feature.
- **Bullets and short paragraphs.** No walls of text.
- **Lead every section with the outcome, not the process.** "Cuts manual lead triage to zero" beats "Uses a Claude API call to score leads."
- **Use the buyer's vocabulary, not the engineer's.** Say "leads" not "rows in the signals table." Say "your CRM" not "the integration layer."
- **Name specific tools the buyer recognizes.** Claude, OpenAI, HubSpot, Salesforce, Airtable, Slack, Notion, Stripe, Postgres, Next.js, n8n, Zapier, Make. Drop niche frameworks unless they are the point.
- **Prefer concrete numbers.** "Top-5 NSE large caps, 4 cron jobs, every-minute polling, 6-table Postgres schema" beats "comprehensive market data pipeline."
- **Frame no-code rescues explicitly when relevant.** Phrase to reuse: *"Replaces fragile Zapier/n8n flows with a proper service the team can actually trust."*
- **Don't apologize or hedge.** No "small project", "side project", "just a demo", "POC quality". If it ran in production or solves a real workflow, it counts.
- **Don't dump the full architecture.** Pick the 2–3 details that prove engineering judgment (idempotency, dedup, retries, cost caps, security boundary, observability) and skip the rest.
- **Don't list every dependency.** The Stack line is buyer-facing, not a `package.json` dump.
- **No emojis. No marketing adjectives** ("cutting-edge", "robust", "seamless", "powerful", "innovative"). Replace them with a specific fact.

---

## 5. Title patterns that work

Pick the closest match and adapt:

- `<Outcome> — <Primary tech> + <Integration> (<format: Live Demo / Production / Case Study>)`
  - e.g. *AI Lead Qualifier — Claude API + Airtable + Slack (Live Demo)*
- `<Domain> <System Type> with <AI capability>`
  - e.g. *Intraday Trading Bot with Claude-Driven Decision Gate*
- `<Tool> Implementation for <Stack/Domain>`
  - e.g. *Better-Auth Implementation for Next.js SaaS*
- `<Broken thing> → <Fixed thing>: <Stack>`
  - e.g. *Zapier Flow → Python Service: HubSpot ↔ Stripe ↔ Slack*

Avoid titles that start with "A" or "The". Avoid the word "Custom" unless it earns its place.

---

## 6. Buyer wedges to position into

If the project plausibly fits one of these, say so explicitly in the Problem or Solution section — these are the searches Upwork buyers actually run:

1. **AI agents over CRM / ops data** — read records, draft outreach, qualify, update fields.
2. **RAG over company docs / SOPs** — ask questions of your own knowledge base.
3. **Document / data automation** — invoices, contracts, reports parsed by LLM + routed.
4. **AI-summarized internal dashboards** — operational data explained in plain English.
5. **n8n / Make / Zapier rescue** — replacing fragile no-code with a proper service.
6. **API integration glue** — CRM + accounting + PM + AI in one workflow.
7. **Custom internal tools / web apps** — Next.js / React with AI features built in.

If the project does *not* fit any of these, write the case study as-is but ensure the Result section ends with a reusability hook that bridges to one of them (e.g. "Same scheduler + LLM-gated decision pattern works for any rules-plus-judgment workflow — lead qualification, support triage, content moderation.").

---

## 7. What to leave out

- Internal codenames, ticket IDs, sprint numbers.
- Stack pieces that are noise on a senior profile (raw HTML, CSS, jQuery, generic JavaScript).
- Limitations of free/POC tier ("we used yfinance because the paid data feed is out of scope") unless framed as a deliberate engineering trade-off.
- Anything under NDA or anything that names a real private client without permission.
- Your own learning journey. The buyer does not care what you learned. They care what they get.

---

## 8. Final self-check before returning the entry

Run through this list. Fix any "no":

- [ ] Does the Title state an outcome a non-technical buyer understands?
- [ ] Does the Problem describe a *business* pain, not a technical task?
- [ ] Does the Solution name at least one trust signal (idempotency, retry, dedup, cost cap, observability, audit log)?
- [ ] Does the Result contain at least one concrete fact (number, cadence, uptime, review, scope)?
- [ ] Is the entry within 400–600 words, with production context inside Result and features inside Solution (not split into separate top-level sections)?
- [ ] Does the Stack line lead with AI/automation, not the framework?
- [ ] Is there a reusability hook in the last sentence?
- [ ] Zero marketing adjectives, zero emojis, zero hedging?
- [ ] Is the asset checklist in Section 9 satisfied (at minimum: cover + architecture + one real screenshot)?

If all yes, output the entry. If anything is no, fix it before returning.

---

## 9. Image assets to generate

A portfolio entry without images is a wall of text. Upwork allows multiple images per portfolio piece. Use them. Produce at least three, ideally five. The case study should also output an **`Image assets:`** block listing each asset with a one-line spec so the user knows exactly what to make.

### Required: Asset 1 — Cover image

The thumbnail in profile listings. This decides whether anyone clicks. Pick ONE based on the project:

- **Architecture diagram** if the work is integration / pipeline / multi-service. 4–7 boxes max, labeled arrows, one data store, one LLM step, one action layer.
- **Dashboard screenshot** if there's a real UI. Hide PII. Crop tight on the single most impressive view.
- **Before / After split** if the work is a rescue or refactor. Left side broken (red, tangled). Right side clean.

Constraints:
- 16:9 ratio, at least 1200×675 px. Smaller images get upscaled and look bad.
- Light backgrounds. Upwork's chrome is light and dark images fight it.
- Project name top-left in a clean sans-serif if the visual doesn't already say it.
- No fake data passed off as real. Label sample data "Sample" in a corner.

### Required: Asset 2 — System architecture diagram

Even when Asset 1 is a screenshot, a system diagram has to exist as Asset 2. Buyers who click want to see *how* it works. Required components, in priority order:

1. **The trigger** — cron / webhook / user action / scheduled job. Top or left edge.
2. **The data store** — Postgres / Airtable / vector DB / Redis. Center.
3. **The LLM step** — drawn as its own box, with the model name written on it (e.g. "Claude Sonnet 4.6"). Not just "AI".
4. **The action layer** — Slack message, CRM write, broker order, email send. Bottom or right edge.
5. **The guard rails** — risk gate, validator, idempotency check. Drawn as a *gate the flow passes through*, not as an aside.

Arrows must be labeled with what passes between boxes ("1-min OHLC", "context JSON", "TAKE/SKIP verdict"). Unlabeled arrows are noise.

Tools, ranked:
- **Excalidraw** (excalidraw.com) — fastest, hand-drawn aesthetic, exports clean PNG. Default choice.
- **Eraser.io** — diagrams-as-code, polished look, free tier is enough.
- **draw.io / diagrams.net** — most powerful, slower, exports cleanly.
- Avoid: PowerPoint (corporate), Lucidchart free tier (watermarks on export), Mermaid embedded in markdown (buyers don't render markdown).

### Required: Asset 3 — Real screenshot of the working system

Proof the thing exists. Options:
- UI page screenshot (one or two, not five).
- Terminal output from a real cron run.
- A row of real produced output (a generated retro, a sent Slack message, an Airtable record).

Hide: customer names, real keys, real account balances. Blur or replace, don't omit, so the screenshot still looks like a screenshot.

### Optional but high-impact: Asset 4 — Sample output

If the system produces user-visible text (LLM summaries, drafted emails, classified records, retrospectives, scored leads), screenshot ONE end-to-end example. Label the input and the output. This is the single most persuasive piece of evidence for AI work — buyers literally see what they would get.

Format: two-column side-by-side, "Input" left, "Output" right, or top/bottom. Use a real example, redact identifiers.

### Optional: Asset 5 — Code excerpt

A 15–30 line screenshot of one well-chosen file. Pick a file that shows engineering judgment, not boilerplate:
- The system prompt for the LLM step.
- The risk gate or validator.
- The idempotency / dedup logic.
- The JSON schema for structured output.

NOT package.json. NOT a CRUD route. NOT 200 lines of imports.

Tools: **carbon.now.sh** or **ray.so**. Settings: window controls off, dark theme (One Dark, Dracula, or Night Owl), no background gradient, no drop shadow. Plain export, transparent background where possible.

### File naming + location

Save under `case-study-assets/` at the repo root:

- `case-study-assets/cover.png`
- `case-study-assets/architecture.png`
- `case-study-assets/screenshot-<feature>.png`
- `case-study-assets/sample-output-<feature>.png`
- `case-study-assets/code-<file>.png`

### What to skip

- Stock photos. Never.
- Generic AI-generated hero images. They read as low-effort.
- Animated GIFs. Upwork's listing grid won't play them.
- Watermarked exports.
- A wall of five UI screenshots that all look the same. Three differentiated assets beat five repetitive ones.

### Output format inside the case study

After the `PORTFOLIO ENTRY` block, append a second block tagged `IMAGE ASSETS` listing each asset to produce, in order, with: filename, one-line "what to show", suggested tool. Example:

```
IMAGE ASSETS

1. case-study-assets/cover.png
   What: 16:9 architecture diagram, four cron jobs feeding one Postgres, Claude box highlighted, dashboard on top.
   Tool: Excalidraw, 15 min.

2. case-study-assets/architecture.png
   What: Detailed version of the cover. Label every arrow with the payload it carries.
   Tool: Excalidraw, 30 min.

3. case-study-assets/screenshot-dashboard.png
   What: The Summary page mid-trading-day. Crop to the open-positions table + today's P&L.
   Tool: Real screenshot.

4. case-study-assets/sample-output-retro.png
   What: One closed-trade retro showing the layman summary + verdict label + learnings.
   Tool: Real screenshot.

5. case-study-assets/code-system-prompt.png
   What: The decider system prompt, lines 67–96. Dark theme, no shadow.
   Tool: carbon.now.sh.
```
