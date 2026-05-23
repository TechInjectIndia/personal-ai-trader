# Context Engine — Sprint PRD & Tracker

> Living checklist for the Context Engine build (spec: [`context-engine-prd.md`](./context-engine-prd.md)).
> Each task carries two states I update as work lands. Branch: `feat/competition-league` (commit per task, never push — human pushes).

## Status legend

| Mark | Meaning |
|---|---|
| ☐ | not started |
| 🟡 | in progress |
| ✅ | done |

**Definition of Developed:** code written · `ruff check` + `mypy helm` clean · `init_schema()` still idempotent · committed locally.
**Definition of Tested:** pytest/unit or integration coverage where meaningful · behaviour verified by a real run · no regression in the live loop.

## Progress summary

| Phase | Developed | Tested |
|---|---|---|
| C1 Context Engine MVP | 0 / 8 | 0 / 8 |
| C2 Catalyst signals | 0 / 5 | 0 / 5 |
| C3 Conviction overlay | 0 / 4 | 0 / 4 |
| C4 League differentiator | 0 / 4 | 0 / 4 |
| C5 Surface & learn | 0 / 4 | 0 / 4 |
| **Total** | **0 / 25** | **0 / 25** |

---

## C1 — Context Engine MVP

Foundations: news + regime ingestion → injected into the house decider.

| # | Task | Files | Dev | Test |
|---|---|---|:--:|:--:|
| C1.1 | Schema: `news_items` + `market_context` tables (idempotent) | `helm/data/schema.sql` | ☐ | ☐ |
| C1.2 | `NewsSource` interface + free adapters (Google/ET/MC RSS, yfinance regime) | `helm/context/news_source.py` | ☐ | ☐ |
| C1.3 | Regime fetch → `market_context` (GIFT-Nifty, VIX, USDINR, crude, global close) | `scripts/ingest_context.py` | ☐ | ☐ |
| C1.4 | RSS news fetch → `news_items` with `dedup_key` | `scripts/ingest_context.py` | ☐ | ☐ |
| C1.5 | Local lexicon sentiment scoring ($0) for rolling feed | `helm/context/sentiment.py` | ☐ | ☐ |
| C1.6 | Pre-open LLM brief (prompt-cached) → `market_context.brief` + `risk_state` | `scripts/ingest_context.py` | ☐ | ☐ |
| C1.7 | Inject `context` block into house decider prompt | `scripts/decide_signals.py` | ☐ | ☐ |
| C1.8 | Cron wiring (no-op off-hours) + `WORKAROUNDS.md` entry for free news | crontab, `WORKAROUNDS.md` | ☐ | ☐ |

## C2 — Catalyst signals (news generates trades)

| # | Task | Files | Dev | Test |
|---|---|---|:--:|:--:|
| C2.1 | Catalyst producer: unconsumed `news_items` → candidate `signals` | `helm/context/catalyst.py` | ☐ | ☐ |
| C2.2 | Wire producer into scan loop alongside strategies | `scripts/scan_signals.py` | ☐ | ☐ |
| C2.3 | Entry/stop/target convention (market entry, ATR/% stop+target) | `helm/context/catalyst.py` | ☐ | ☐ |
| C2.4 | Dedup (`consumed`) + reuse `per_symbol_cooldown_min` / per-day cap | `helm/context/catalyst.py` | ☐ | ☐ |
| C2.5 | Verify catalyst flows decider → risk gate → `paper_execute` | integration | ☐ | ☐ |

## C3 — Conviction overlay (Bloomberg seed)

| # | Task | Files | Dev | Test |
|---|---|---|:--:|:--:|
| C3.1 | Schema: `fundamental_scores` + `events` tables (idempotent) | `helm/data/schema.sql` | ☐ | ☐ |
| C3.2 | One-time loader: `bloomberg.xlsx` → `fundamental_scores` (ISIN→NSE, revision_trend) | `scripts/seed_fundamentals.py` | ☐ | ☐ |
| C3.3 | Inject conviction **bias flag** (not price) into decider | `scripts/decide_signals.py` | ☐ | ☐ |
| C3.4 | Basic events loader (results/RBI/expiry) → size-down/flatten awareness | `scripts/seed_fundamentals.py` | ☐ | ☐ |

## C4 — League differentiator (context as variable)

| # | Task | Files | Dev | Test |
|---|---|---|:--:|:--:|
| C4.1 | Schema: `competitors.context_level` (`blind`/`full`, default `full`) | `helm/data/schema.sql` | ☐ | ☐ |
| C4.2 | Seed `claude-blind` (T0) + `claude-full` (T3) entrants | `scripts/seed_competitors.py` | ☐ | ☐ |
| C4.3 | Conditional context injection in `build_snapshot` (full only) | `helm/competition/runner.py` | ☐ | ☐ |
| C4.4 | Scoreboard tags each entrant's tier | `helm/competition/*`, dashboard | ☐ | ☐ |

## C5 — Surface & learn

| # | Task | Files | Dev | Test |
|---|---|---|:--:|:--:|
| C5.1 | Dashboard "Context & Catalysts" page (regime banner, per-symbol news/conviction) | `helm/dashboard/pages/` | ☐ | ☐ |
| C5.2 | Scoreboard shows context tier per entrant | `helm/dashboard/pages/` | ☐ | ☐ |
| C5.3 | Persist context-at-entry into `trade_retrospectives` | `helm/retro.py` | ☐ | ☐ |
| C5.4 | Grader asks "did context predict the outcome?" → proposals | `helm/retro.py` | ☐ | ☐ |

---

## Open items to resolve during build (from PRD §7)

- [ ] Catalyst sentiment/relevance thresholds for emission.
- [ ] Headline → symbol extraction: NER vs watchlist string-match.
- [ ] Whether the house bot also gets a `blind`/`full` toggle for parity with the league.

## Changelog

- 2026-05-23 — tracker created; all tasks ☐. Decisions D1–D6 locked in PRD.
