# Helm — Context Engine PRD (News + Conviction layer)

| | |
|---|---|
| **Product** | Helm Context Engine — exogenous context (news + analyst conviction) for the trading loop |
| **Status** | 📋 Planned. Decisions locked 2026-05-23. Sections tagged ✅ Built · 🚧 In dev · 📋 Planned |
| **Owner** | TechInject (single operator / judge) |
| **Author** | Engineering delegate (Claude) |
| **Last updated** | 2026-05-23 |
| **Depends on** | Layer 1 trading loop (✅), Layer 3 competition league (✅, `feat/competition-league`) |
| **Source of truth for config** | `helm/config.py` (hardcoded by design) |

> Today both deciders — the house `decide_signals.py` and all competition agents in `competition/runner.py` — reason over **price candles + portfolio/risk state only**. The bot sees the chart but not the world. This PRD adds an **exogenous Context layer** with two feeds: a real-time **news/catalyst** axis and a slow-moving **analyst-conviction** axis (seeded from `bloomberg.xlsx`). News doesn't just gate trades — it *generates* candidate signals. And context access is deliberately made an **experimental variable** across the league.

---

## 1. Strategic frame

The bot is technically blind. The two requested features — "feed in the world-news factor" and "learn from the Bloomberg report" — are the same gap: no exogenous context. We build them as **one layer, two feeds**:

- **News = the real-time catalyst axis.** Macro/regime, per-symbol catalysts, scheduled events.
- **Bloomberg = the slow-moving conviction axis.** Analyst Buy/Hold/Sell consensus, implied upside, estimate-revision trend, target dispersion.

Both feed every decider. Together they answer: *given the chart says "breakout," does the world agree?*

## 2. Decisions locked (2026-05-23)

| # | Decision | Choice |
|---|---|---|
| D1 | Role of news | **Context overlay AND signal generator** — news both colours TAKE/SKIP and proposes new entries |
| D2 | Data sourcing | **Free-only** (Google News RSS, ET/Moneycontrol RSS, yfinance regime) behind a swappable `NewsSource` interface; logged in `WORKAROUNDS.md` |
| D3 | Fundamental source | **One-time static seed** from `bloomberg.xlsx` → `fundamental_scores`; live source deferred |
| D4 | League context | **Context-access as a differentiator.** Binary tier: `blind` vs `full` |
| D5 | Tier assignment | **`claude-blind` (T0) + `claude-full` (T3); gemini/qwen/codex/opencode all full.** 6 entrants |
| D6 | Target anchor | **Bloomberg = directional bias flag only.** Stops/targets stay technical (ATR/%). Stale 2023 numbers must not anchor live exits |

The experiment from D5 yields **both axes from one setup**: `claude-blind` vs `claude-full` isolates the *context effect* on a fixed model; `claude-full` vs the four other full agents isolates the *model effect* at constant context.

## 3. Data model (additions)

All idempotent (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`), `timestamptz`, IST boundaries, `Decimal` money. No new YAML/JSON config — knobs go in `helm/config.py`.

- **`news_items`** — `id, ts, symbol TEXT NULL, source, headline, url, raw JSONB, sentiment NUMERIC NULL, catalyst_type TEXT NULL, dedup_key TEXT UNIQUE, consumed BOOL DEFAULT FALSE`. `symbol NULL` = macro/market-wide. `consumed` = the catalyst-producer dedupe boundary (mirrors `signals.consumed`).
- **`market_context`** — `id, ts, risk_state TEXT, gift_nifty, india_vix, usdinr, crude, global_close JSONB, brief TEXT, raw JSONB`. One row per refresh; `brief` holds the pre-open LLM summary.
- **`events`** — `id, symbol TEXT NULL, event_type TEXT, event_date DATE, detail TEXT, source`. Calendar: results / RBI / expiry / budget / ex-div.
- **`fundamental_scores`** — `symbol PK, buy_ratio, implied_upside, target_avg, target_median, target_high, target_low, revision_trend TEXT, dispersion_bucket TEXT, conviction_score, as_of_date DATE, source TEXT`. Bloomberg seed stamped `source='bloomberg_seed_2023'`.
- **`competitors`** — add `context_level TEXT DEFAULT 'full'` (`'blind'|'full'`).
- **`signals`** — no schema change; new `strategy` value `'news_catalyst'`. (`competitor_id` already nullable from J1.)

## 4. Components

1. **`helm/context/news_source.py`** — `NewsSource` ABC + free adapters (Google News RSS per symbol, ET/Moneycontrol RSS, yfinance regime: GIFT-Nifty proxy, India VIX, USD/INR, crude, global close). Black-box so a paid API drops in later. → `WORKAROUNDS.md`.
2. **`scripts/ingest_context.py`** (cron) — fetch news → `news_items` (dedup on `dedup_key`); fetch regime → `market_context`; score sentiment with a **local lexicon (VADER/finBERT-lite, $0)** for the rolling feed; one **LLM brief** at pre-open (~08:45 IST, prompt-cached) → `market_context.brief` + `risk_state`. No-op outside market window/weekends.
3. **`helm/context/catalyst.py`** — catalyst signal **producer** (NOT a `Strategy` — keeps the `scan()` ABC DB-free). Run inside `scan_signals.py` alongside strategies: read unconsumed `news_items` above sentiment/relevance threshold → emit `signals(strategy='news_catalyst', side, entry=market, stop=ATR, target=ATR/%)`; mark item `consumed`. Flows through the **same decider → risk gate → `paper_execute` chokepoint** — a headline never bypasses risk. Dedupe per `(symbol, dedup_key)`; respect existing `per_symbol_cooldown_min` + `max_signals_per_symbol_per_day`.
4. **`scripts/seed_fundamentals.py`** — one-time loader: `bloomberg.xlsx` → `fundamental_scores`, mapped **ISIN → NSE symbol**. Re-runnable upsert. Computes `revision_trend` from the four dated target columns (Jun'22→Mar'23).
5. **Context injection**
   - House decider (`decide_signals._build_user_prompt`): add a `context` block — regime + this symbol's latest catalyst + conviction bias flag.
   - Competition runner (`build_snapshot`): inject the `context` block **only when `competitor.context_level == 'full'`**.
6. **Dashboard** — new **"Context & Catalysts"** page (regime banner, per-symbol news/sentiment/conviction). Scoreboard tags each entrant's tier so `claude-blind` vs `claude-full` reads at a glance.
7. **Retro loop** — feed the news/conviction that was visible at entry into `trade_retrospectives` so the grader can ask *"did context predict the outcome?"* — closes the learning loop and generates `improvement_proposals`.

## 5. Cadence & cost ($0 maintained)

| Job | Cadence (IST) | Cost |
|---|---|---|
| `ingest_context.py` — regime + RSS | every 5–15 min in window | free (RSS + yfinance) |
| `ingest_context.py` — pre-open brief | once ~08:45 | 1 LLM call, prompt-cached |
| Sentiment scoring (rolling) | per item | free (local lexicon) |
| `seed_fundamentals.py` | one-time | free |

Only marginal LLM cost is the single pre-open brief; catalyst signals reuse the existing decider budget.

## 6. Phasing

- **C1 — Context Engine MVP** 📋 — `news_items` + `market_context` + regime + pre-open brief → injected into the house decider.
- **C2 — Catalyst signals** 📋 — `news_catalyst` producer → `signals` → decider/risk gate. (Delivers D1's signal-generation half.)
- **C3 — Conviction overlay** 📋 — Bloomberg seed → `fundamental_scores` → bias flag in decider (D6).
- **C4 — League differentiator** 📋 — `context_level` on `competitors`; conditional injection; `claude-blind` + `claude-full` entrants; scoreboard tier tags (D4/D5).
- **C5 — Surface & learn** 📋 — dashboard Context page; retro integration.

## 7. Risks & open items

- **RSS reliability/coverage** (free tier) — black-boxed; swap to paid API later (`WORKAROUNDS.md`).
- **Lexicon sentiment quality** — acceptable for POC; upgrade to LLM/finBERT scoring later.
- **Stale Bloomberg seed** — mitigated by D6 (bias flag, not price anchor); refresh when a live source lands.
- **News-as-trigger noise/latency** — every catalyst still passes the risk gate + decider; cooldowns and per-symbol caps bound frequency.
- **6th entrant** — `claude-blind` adds calls on the subscription path; negligible cost.
- **Open:** exact sentiment/relevance thresholds for catalyst emission; symbol-mention extraction from headlines (NER vs watchlist string-match); whether the house bot also gets a `blind`/`full` toggle for parity with the league.
