# FRD F7 — Context Engine (news/event conviction layer)

_Phase 3 · Owner: PM + house engineer loop · Status: proposed · Builds on: docs/prd/context-engine-prd.md_

## 1. Problem
Top-5 NSE mega-caps on price-only, no-volume 1-min data are the most efficient instruments on the exchange — there is little exploitable edge in the chart alone (proven: ~0 gross edge across 189 trades). The only credible source of **genuine** intraday edge is **information the chart lacks**: news, corporate events, sector/macro catalysts, and flow. F1–F6 stop us paying to lose; F7 is the bet that actually *creates* edge.

## 2. Goal & success metrics
A conviction layer that ingests external context per symbol, scores it, and feeds it to the deciders (and can itself generate signals).
- Driver: a context-aware decider/strategy shows **positive gross expectancy out-of-sample** with E2C ≥ 3 (measured in F1).
- League differentiator: `claude-blind` (no context) vs `claude-full` (context) — does context lift expectancy?
- Guardrail: cost-bounded ingestion (free/cheap sources first); black-box interface so the source can be swapped.

## 3. HLD
Three sub-phases, each independently shippable and measurable:

```
P3a INGEST+SCORE (shadow):  news/event sources ─► context_items ─► LLM score ─► context_scores(symbol, ts, score, rationale)
P3b FEED DECIDER:           decide_signals prompt += latest context_score(symbol)   (A/B: blind vs full)
P3c CONTEXT SIGNALS:        a ContextStrategy emits signals on strong fresh catalysts (long-only, E2C-gated)
```
- **Ingestion** behind an interface `ContextSource.fetch(symbol, since) -> list[ContextItem]` so we can start with free RSS/news scrapes and later swap in a paid feed without touching consumers (per the project's WORKAROUNDS black-box ethos).
- **Scoring** via the existing `helm/llm.py` adapter (cache the system prompt) → a directional conviction score per symbol with rationale + freshness/decay.
- **Consumption** is additive: the decider gets a context block; a new strategy can gate on it. Both measured against blind baselines.

## 4. LLD
**Schema (`helm/data/schema.sql`) — new tables:**
```sql
CREATE TABLE context_items (              -- raw ingested items
  id BIGSERIAL PRIMARY KEY, symbol TEXT, source TEXT, url TEXT,
  headline TEXT, body TEXT, published_ts TIMESTAMPTZ, ingested_ts TIMESTAMPTZ DEFAULT now(),
  UNIQUE(symbol, url));
CREATE TABLE context_scores (             -- LLM-scored conviction per symbol
  id BIGSERIAL PRIMARY KEY, symbol TEXT, scored_ts TIMESTAMPTZ DEFAULT now(),
  score NUMERIC(4,3),                     -- -1.000 (bearish) .. +1.000 (bullish)
  half_life_min INT, rationale TEXT, item_ids BIGINT[]);
CREATE INDEX context_scores_recent ON context_scores(symbol, scored_ts DESC);
```
**Modules:**
- `helm/context/sources/` — `ContextSource` ABC + a free RSS/news implementation; no secrets beyond optional API keys in `.env`.
- `helm/context/score.py` — `score_symbol(symbol, items) -> ContextScore` via `llm.complete` with a strict JSON schema `{score, half_life_min, rationale}`.
- `helm/context/read.py` — `current_context(symbol) -> Decimal | None` applying time-decay: `score * 0.5 ** (age_min / half_life_min)`; None if stale/absent.
- `scripts/ingest_context.py` — cron (in-window, every ~15 min): fetch → upsert `context_items` → score new → write `context_scores`.

**P3b decider integration (`scripts/decide_signals.py`):** add a `CONTEXT: {score, rationale}` block to the user prompt when `current_context(symbol)` is not None. A/B: run a `claude-blind` competitor without it. **No** auto-trade purely on context in P3b.

**P3c context strategy:** `helm/strategies/context_momentum.py` — emits a long signal when `current_context >= CONTEXT_SIGNAL_THRESHOLD` AND price confirms; pure w.r.t. candles + an injected context value (keep the DB read in `scan_signals`, passing context in, to honor "no DB in strategies" — pass `context=current_context(symbol)` as a scan kwarg or pre-resolved map). E2C-gated by F2; target-floored by F4.

## 5. Test plan
- `ContextSource` parsing (fixture feeds → items, dedupe on url).
- `score.py` schema-validated output; decay math in `read.py` (half-life, staleness → None).
- Decider prompt includes/excludes context block correctly (A/B flag).
- Context strategy fires only above threshold + price confirm; once-per-trigger guard.
- Backtest/replay: score historical items, measure would-be expectancy before live.

## 6. Rollout / revert
Ship P3a in **shadow** (ingest + score, no trading) for ≥2 weeks; validate score↔next-move correlation in F1-style analysis. Then P3b A/B. Then P3c. Each sub-phase reverts independently (drop the prompt block / remove the strategy from ACTIVE). Tables are additive.

## 7. Risks
- **Cost/rate-limits** on news + scoring LLM calls — bound by symbol count (5) and cadence; cache aggressively.
- **Garbage edge / overfitting to headlines** — shadow-validate before trading; require out-of-sample positive expectancy.
- **Latency** — intraday news edge decays fast; ingest cadence must be tight enough (≤15 min) or the edge is gone.
- **Source reliability** — black-box interface lets us swap sources without rework.
