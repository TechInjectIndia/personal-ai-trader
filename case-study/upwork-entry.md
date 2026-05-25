> Refreshed Upwork portfolio entry covering all three layers (the original root `case-study.md` only described Layer 1). Paste the block below into Upwork. Diagram PNGs are ready in `case-study/assets/` (Upwork can't render Mermaid); the IMAGE ASSETS block also lists the dashboard screenshots to add.

```
PORTFOLIO ENTRY

Title: AI Agents That Decide, Grade, and Ship Their Own Code (Production)

Cover image: the Competition League dashboard screenshot (case-study/assets/screenshot-hero.png) — six AI agents with personas, status, and per-agent teams. Alternative: the platform diagram (case-study/assets/04-platform-overview.png).

Problem:
Most automation is dumb rules: a trigger fires, the system acts, and nobody asks whether the action made sense until it has already cost money. The operator gets no explanation of why each call was made, and no path from "we made a mistake" to "the system is fixed." I wanted automation that keeps the fast, cheap, deterministic rule layer but wraps real judgment around it — an AI that vets each action before it happens, grades each outcome in plain English afterward, and an AI engineering team that turns those lessons into shipped code.

Solution:
Helm is a production system with three AI agent layers sharing one PostgreSQL backend, cron-driven on a single Linux server.
1. Decision gate: mechanical strategies propose actions; Claude rules TAKE/SKIP with reasoning on each one; a hard-coded validator (not the LLM) has the final word; a second Claude call writes a plain-English retrospective with a verdict and concrete learnings. Every decision — including every SKIP and every block — is one auditable row.
2. Self-improvement loop, per agent: graded mistakes become a ranked queue that three cooperating agents work — a PM agent triages it into typed tasks (with a semantic pass that dedups hundreds of near-identical proposals), an Engineer agent ships one change at a time through a closed set of schema-validated code mutators (never free-form), runs lint/tests as a hard gate and commits, and a Tester agent verifies on the live deployment and auto-reverts regressions so main stays green. Every competing agent runs its own copy of this loop. The human is the only gate that pushes.
3. Competition league: six different AI backends (Claude, Gemini, Qwen3, Nemotron, opencode, Kiro) each trade an isolated ₹50k paper wallet — each racing to double it — through the same risk-gated path, each with a distinct trading persona, ranked on a leaderboard with a "how it thinks" view and per-agent PM/Dev/Tester teams.

The trust signals that make it production-grade, not a demo: idempotent dedup (a consumed flag flipped inside the same transaction as the decision row); race-proofing via Postgres advisory locks and FOR UPDATE SKIP LOCKED task claiming; a pure-code risk gate the model cannot bypass; per-backend quota handling with pause and auto-resume; a tolerant JSON parser for when the model wraps output in markdown; a full audit trail on every state change; a live System Map that rebuilds the codebase into a navigable knowledge graph on every commit; and all LLM traffic behind one swappable transport so the POC runs at near-zero cost and flips to a paid API with a single env var.

Result:
Runs unattended every weekday and silently no-ops outside hours. ~16,700 lines of Python, a 20-table Postgres schema, a 10-page dashboard, six competing AI models, deployed behind PM2 + nginx + Let's Encrypt on one box — no queue, no Redis, no async sprawl. Every signal yields exactly one explained decision; every closed action yields exactly one plain-English grade; every shipped change self-verifies or reverts. The reusable asset is the pattern, not the trading code: deterministic producers propose, an AI judges, a hard validator guards, a second AI grades, agents improve the loop. The same scaffold drops into lead qualification, support-ticket triage, document review, and content moderation — anywhere a stream of decisions needs judgment before the action and explanation after it.

Stack: Claude (Anthropic API + CLI), multi-LLM orchestration (Gemini/Qwen3/Nemotron/opencode/Kiro), Python 3.11, PostgreSQL, Streamlit, cron, PM2, nginx.

Live demo: helm.techinject.co.in (operator login). Loom walkthrough available on request.
Repo: Private — code walkthrough on request.
```

```
IMAGE ASSETS

Diagrams — ready-made PNGs (also live as Mermaid on the website):
1. case-study/assets/04-platform-overview.png  (READY)
   What: Three AI agent layers on one Postgres backend. Good cover/thumbnail.
2. case-study/assets/01-decision-pipeline.png  (READY)
   What: Layer 1 — signal → Claude decide → risk gate → trade → Claude retro, labeled.
3. case-study/assets/02-self-improvement-loop.png  (READY)
   What: Layer 2 — PM → Engineer → Tester → human push, the always-green agent loop.
4. case-study/assets/03-competition-league.png  (READY)
   What: Layer 3 — five AI backends, one shared risk gate, isolated wallets, leaderboard.

Screenshots — captured from the live dashboard, no PII (all READY in case-study/assets/):
5. screenshot-hero.png  (READY — strongest cover)
   What: Competition League page — six AI agents, each with avatar, persona, live status, equity, and its own PM/Dev/Tester team. Most clickable thumbnail.
6. screenshot-self-improvement.png  (READY)
   What: Self-Improvement Loop page — per-agent dropdown, goal progress chart, and the open proposal / task / release queues.
7. screenshot-retro-expanded.png  (READY — highest-impact)
   What: One expanded retrospective: verdict badge (Bad call on a SKIP), plain-English summary, why we acted, what happened, verdict reasoning, learnings. A non-obvious verdict so the AI is visibly doing judgment, not echoing the P&L sign.
8. screenshot-system-map.png  (READY)
   What: System Map page — the codebase as a live, clustered knowledge graph (~1,060 nodes / ~1,700 edges / ~66 subsystems), rebuilt on every commit.
9. screenshot-retros.png  (READY — optional)
   What: Retrospectives page — the five-way verdict distribution (Good call / Bad call / Lucky / Unlucky / Mixed) across all reviews, with per-agent filter.

Note: the Summary / home page shows the broker's real name + Kite account ID — it was deliberately excluded from all captures.
```
